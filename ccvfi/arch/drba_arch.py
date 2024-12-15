import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from ccvfi.arch.arch_utils.warplayer import warp
from ccvfi.arch import ARCH_REGISTRY
from ccvfi.type import ArchType

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

@ARCH_REGISTRY.register(name=ArchType.DRBA)
class DRBA(nn.Module):
    def __init__(self,
                 support_cupy=False,
                 ):
        super(DRBA, self).__init__()
        self.block0 = IFBlock(7 + 32, c=192)
        self.block1 = IFBlock(8 + 4 + 8 + 32, c=128)
        self.block2 = IFBlock(8 + 4 + 8 + 32, c=96)
        self.block3 = IFBlock(8 + 4 + 8 + 32, c=64)
        self.block4 = IFBlock(8 + 4 + 8 + 32, c=32)
        self.encode = Head()
        if support_cupy:
            from ccvfi.arch.arch_utils.softsplat import softsplat as fwarp
            self.fwarp = fwarp
        else:
            from ccvfi.arch.arch_utils.softsplat_torch import softsplat as fwarp
            self.fwarp = fwarp

    def inference(self, x, timestep=0.5, scale_list=[16, 8, 4, 2, 1], training=False, fastmode=True, ensemble=False):
        if training == False:
            channel = x.shape[1] // 2
            img0 = x[:, :channel]
            img1 = x[:, channel:]
        if not torch.is_tensor(timestep):
            timestep = (x[:, :1].clone() * 0 + 1) * timestep
        f0 = self.encode(img0[:, :3])
        f1 = self.encode(img1[:, :3])
        flow_list = []
        merged = []
        mask_list = []
        warped_img0 = img0
        warped_img1 = img1
        flow = None
        mask = None
        block = [self.block0, self.block1, self.block2, self.block3, self.block4]
        for i in range(5):
            if flow is None:
                flow, mask, feat = block[i](torch.cat((img0[:, :3], img1[:, :3], f0, f1, timestep), 1), None,
                                            scale=scale_list[i])
                if ensemble:
                    print("warning: ensemble is not supported since RIFEv4.21")
            else:
                wf0 = warp(f0, flow[:, :2])
                wf1 = warp(f1, flow[:, 2:4])
                fd, m0, feat = block[i](
                    torch.cat((warped_img0[:, :3], warped_img1[:, :3], wf0, wf1, timestep, mask, feat), 1), flow,
                    scale=scale_list[i])
                if ensemble:
                    print("warning: ensemble is not supported since RIFEv4.21")
                else:
                    mask = m0
                flow = flow + fd
            mask_list.append(mask)
            flow_list.append(flow)
            warped_img0 = warp(img0, flow[:, :2])
            warped_img1 = warp(img1, flow[:, 2:4])
            merged.append((warped_img0, warped_img1))
        mask = torch.sigmoid(mask)
        merged[4] = (warped_img0 * mask + warped_img1 * (1 - mask))
        if not fastmode:
            print('contextnet is removed')
            '''
            c0 = self.contextnet(img0, flow[:, :2])
            c1 = self.contextnet(img1, flow[:, 2:4])
            tmp = self.unet(img0, img1, warped_img0, warped_img1, mask, flow, c0, c1)
            res = tmp[:, :3] * 2 - 1
            merged[4] = torch.clamp(merged[4] + res, 0, 1)
            '''
        return merged[4], flow_list

    def calc_flow(self, a, b, _scale):
        imgs = torch.cat((a, b), 1)
        scale_list = [16 / _scale, 8 / _scale, 4 / _scale, 2 / _scale, 1 / _scale]
        # get top scale flow flow0.5 -> 0/1
        _, flow_list = self.inference(imgs, timestep=0.5, scale_list=scale_list)
        flow = flow_list[-1]
        flow50, flow51 = flow[:, :2], flow[:, 2:]

        # only need forward direction flow
        flow05_primary = self.fwarp(flow51, flow50, None, 'avg')
        flow15_primary = self.fwarp(flow50, flow51, None, 'avg')

        # qvi
        # flow05, norm2 = warp(flow50, flow50)
        # flow05[norm2]...
        # flow05 = -flow05

        flow05_secondary = -self.fwarp(flow50, flow50, None, 'avg')
        flow15_secondary = -self.fwarp(flow51, flow51, None, 'avg')

        _flow01_primary = flow05_primary * 2
        _flow10_primary = flow15_primary * 2

        _flow01_secondary = flow05_secondary * 2
        _flow10_secondary = flow15_secondary * 2

        return _flow01_primary, _flow10_primary, _flow01_secondary, _flow10_secondary

    # Flow distance calculator
    def distance_calculator(self, _x):
        u, v = _x[:, 0:1], _x[:, 1:]
        return torch.sqrt(u ** 2 + v ** 2)

    def forward(self, _I0, _I1, _I2, minus_t, zero_t, plus_t, _left_scene, _right_scene, _scale, _reuse=None):


        flow10_p, flow01_p, flow01_s, flow10_s = self.calc_flow(_I1, _I0, _scale) if not _reuse else _reuse
        flow12_p, flow21_p, flow12_s, flow21_s = self.calc_flow(_I1, _I2, _scale)

        # Compute the distance using the optical flow and distance calculator
        d10_p = self.distance_calculator(flow10_p) + 1e-4
        d12_p = self.distance_calculator(flow12_p) + 1e-4
        d10_s = self.distance_calculator(flow10_s) + 1e-4
        d12_s = self.distance_calculator(flow12_s) + 1e-4

        # Calculate the distance ratio map
        drm10_p = d10_p / (d10_p + d12_p)
        drm12_p = d12_p / (d10_p + d12_p)
        drm10_s = d10_s / (d10_s + d12_s)
        drm12_s = d12_s / (d10_s + d12_s)

        ones_mask = torch.ones_like(drm10_p, device=drm10_p.device)

        def calc_drm_rife(_t):
            # The distance ratio map (drm) is initially aligned with I1.
            # To align it with I0 and I2, we need to warp the drm maps.
            # Note: 1. To reverse the direction of the drm map, use 1 - drm and then warp it.
            # 2. For RIFE, drm should be aligned with the time corresponding to the intermediate frame.
            _drm01r_p = self.fwarp(1 - drm10_p, flow10_p * ((1 - drm10_p) * 2) * _t, None, strMode='avg')
            _drm21r_p = self.fwarp(1 - drm12_p, flow12_p * ((1 - drm12_p) * 2) * _t, None, strMode='avg')
            _drm01r_s = self.fwarp(1 - drm10_s, flow10_s * ((1 - drm10_s) * 2) * _t, None, strMode='avg')
            _drm21r_s = self.fwarp(1 - drm12_s, flow12_s * ((1 - drm12_s) * 2) * _t, None, strMode='avg')

            self.warped_ones_mask01r_p = self.fwarp(ones_mask, flow10_p * ((1 - _drm01r_p) * 2) * _t, None, strMode='avg')
            self.warped_ones_mask21r_p = self.fwarp(ones_mask, flow12_p * ((1 - _drm21r_p) * 2) * _t, None, strMode='avg')
            self.warped_ones_mask01r_s = self.fwarp(ones_mask, flow10_s * ((1 - _drm01r_s) * 2) * _t, None, strMode='avg')
            self.warped_ones_mask21r_s = self.fwarp(ones_mask, flow12_s * ((1 - _drm21r_s) * 2) * _t, None, strMode='avg')

            holes01r_p = self.warped_ones_mask01r_p < 0.999
            holes21r_p = self.warped_ones_mask21r_p < 0.999

            _drm01r_p[holes01r_p] = _drm01r_s[holes01r_p]
            _drm21r_p[holes21r_p] = _drm21r_s[holes21r_p]

            holes01r_s = self.warped_ones_mask01r_s < 0.999
            holes21r_s = self.warped_ones_mask21r_s < 0.999

            holes01r = torch.logical_and(holes01r_p, holes01r_s)
            holes21r = torch.logical_and(holes21r_p, holes21r_s)

            _drm01r_p[holes01r] = (1 - drm10_p)[holes01r]
            _drm21r_p[holes21r] = (1 - drm12_p)[holes21r]

            _drm01r_p, _drm21r_p = map(lambda x: torch.nn.functional.interpolate(x, size=_I0.shape[2:], mode='bilinear',
                                                                                 align_corners=False),
                                       [_drm01r_p, _drm21r_p])

            return _drm01r_p, _drm21r_p

        output1, output2 = list(), list()

        if _left_scene:
            for _ in minus_t:
                zero_t = np.append(zero_t, 0)
            minus_t = list()

        if _right_scene:
            for _ in plus_t:
                zero_t = np.append(zero_t, 0)
            plus_t = list()

        disable_drm = False
        if (_left_scene and not _right_scene) or (not _left_scene and _right_scene):
            drm01r, drm21r = (ones_mask.clone() * 0.5 for _ in range(2))
            drm01r, drm21r = map(lambda x: torch.nn.functional.interpolate(x, size=_I0.shape[2:], mode='bilinear',
                                                                           align_corners=False), [drm01r, drm21r])
            disable_drm = True

        for t in minus_t:
            t = -t
            if t == 1:
                output1.append(_I0)
                continue
            if not disable_drm:
                drm01r, _ = calc_drm_rife(t)
            output1.append(self.inference(torch.cat((_I1, _I0), 1), timestep=t * (2 * drm01r),
                                 scale_list=[16 / _scale, 8 / _scale, 4 / _scale, 2 / _scale, 1 / _scale])[0])
        for _ in zero_t:
            output1.append(_I1)
        for t in plus_t:
            if t == 1:
                output2.append(_I2)
                continue
            if not disable_drm:
                _, drm21r = calc_drm_rife(t)
            output2.append(self.inference(torch.cat((_I1, _I2), 1), timestep=t * (2 * drm21r),
                                 scale_list=[16 / _scale, 8 / _scale, 4 / _scale, 2 / _scale, 1 / _scale])[0])

        _output = output1 + output2

        # next flow10, flow01 = reverse(current flow12, flow21)
        return _output, (flow21_p, flow12_p, flow21_s, flow12_s)

def conv(in_planes, out_planes, kernel_size=3, stride=1, padding=1, dilation=1):
    return nn.Sequential(
        nn.Conv2d(in_planes, out_planes, kernel_size=kernel_size, stride=stride,
                  padding=padding, dilation=dilation, bias=True),
        nn.LeakyReLU(0.2, True)
    )


def conv_bn(in_planes, out_planes, kernel_size=3, stride=1, padding=1, dilation=1):
    return nn.Sequential(
        nn.Conv2d(in_planes, out_planes, kernel_size=kernel_size, stride=stride,
                  padding=padding, dilation=dilation, bias=False),
        nn.BatchNorm2d(out_planes),
        nn.LeakyReLU(0.2, True)
    )


class Head(nn.Module):
    def __init__(self):
        super(Head, self).__init__()
        self.cnn0 = nn.Conv2d(3, 16, 3, 2, 1)
        self.cnn1 = nn.Conv2d(16, 16, 3, 1, 1)
        self.cnn2 = nn.Conv2d(16, 16, 3, 1, 1)
        self.cnn3 = nn.ConvTranspose2d(16, 16, 4, 2, 1)
        self.relu = nn.LeakyReLU(0.2, True)

    def forward(self, x, feat=False):
        x0 = self.cnn0(x)
        x = self.relu(x0)
        x1 = self.cnn1(x)
        x = self.relu(x1)
        x2 = self.cnn2(x)
        x = self.relu(x2)
        x3 = self.cnn3(x)
        if feat:
            return [x0, x1, x2, x3]
        return x3


class ResConv(nn.Module):
    def __init__(self, c, dilation=1):
        super(ResConv, self).__init__()
        self.conv = nn.Conv2d(c, c, 3, 1, dilation, dilation=dilation, groups=1 \
                              )
        self.beta = nn.Parameter(torch.ones((1, c, 1, 1)), requires_grad=True)
        self.relu = nn.LeakyReLU(0.2, True)

    def forward(self, x):
        return self.relu(self.conv(x) * self.beta + x)


class IFBlock(nn.Module):
    def __init__(self, in_planes, c=64):
        super(IFBlock, self).__init__()
        self.conv0 = nn.Sequential(
            conv(in_planes, c // 2, 3, 2, 1),
            conv(c // 2, c, 3, 2, 1),
        )
        self.convblock = nn.Sequential(
            ResConv(c),
            ResConv(c),
            ResConv(c),
            ResConv(c),
            ResConv(c),
            ResConv(c),
            ResConv(c),
            ResConv(c),
        )
        self.lastconv = nn.Sequential(
            nn.ConvTranspose2d(c, 4 * 13, 4, 2, 1),
            nn.PixelShuffle(2)
        )

    def forward(self, x, flow=None, scale=1):
        x = F.interpolate(x, scale_factor=1. / scale, mode="bilinear", align_corners=False)
        if flow is not None:
            flow = F.interpolate(flow, scale_factor=1. / scale, mode="bilinear", align_corners=False) * 1. / scale
            x = torch.cat((x, flow), 1)
        feat = self.conv0(x)
        feat = self.convblock(feat)
        tmp = self.lastconv(feat)
        tmp = F.interpolate(tmp, scale_factor=scale, mode="bilinear", align_corners=False)
        flow = tmp[:, :4] * scale
        mask = tmp[:, 4:5]
        feat = tmp[:, 5:]
        return flow, mask, feat