from typing import Any

from ccvfi import AutoConfig, AutoModel
from ccvfi.config import IFNetConfig
from ccvfi.model import IFNetModel


def test_auto_class_register() -> None:
    cfg_name = "TESTCONFIG.pth"
    model_name = "TESTMODEL"

    cfg = IFNetConfig(
        name=cfg_name,
        url="https://github.com/routineLife1/ccvfi/releases/tag/weights/IFNet_v426_heavy.pkl",
        hash="4cc518e172156ad6207b9c7a43364f518832d83a4325d484240493a9e2980537",
    )

    AutoConfig.register(cfg)

    @AutoModel.register(name=model_name)
    class TESTMODEL(IFNetModel):
        def get_cfg(self) -> Any:
            return self.config

    model: TESTMODEL = AutoModel.from_pretrained(cfg_name)
    assert model.get_cfg() == cfg