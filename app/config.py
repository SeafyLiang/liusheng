import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    data: Path
    music: Path
    inbox: Path
    static: Path
    worker: bool = True

    @classmethod
    def env(cls):
        return cls(
            Path(os.getenv("DATA_DIR", "./runtime/data")).resolve(),
            Path(os.getenv("MUSIC_DIR", "./runtime/music")).resolve(),
            Path(os.getenv("INBOX_DIR", "./runtime/inbox")).resolve(),
            Path(os.getenv("STATIC_DIR", "./frontend/dist")).resolve(),
            os.getenv("WORKER_ENABLED", "1") == "1",
        )

    def prepare(self):
        for p in [self.data, self.music, self.inbox, self.data / "staging", self.data / "covers", self.data / "records"]:
            p.mkdir(parents=True, exist_ok=True)


def within(root: Path, relative: str) -> Path:
    p = root / relative
    if p.is_symlink() or not p.resolve().is_relative_to(root.resolve()):
        raise ValueError("文件路径超出已配置目录")
    return p
