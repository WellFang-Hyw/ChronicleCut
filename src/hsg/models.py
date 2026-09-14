"""数据结构：故事 → 章节 → 分镜。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Scene:
    """最小单元：一段口播 + 一张配图 + 一条字幕。"""

    index: int                     # 全局序号（从 1 开始）
    text: str                      # 口播稿
    image_query: str = ""          # 配图检索词
    caption: str = ""              # 画面上的图注（可为空）
    chapter_index: int = 0
    is_chapter_start: bool = False

    # 运行期产物
    audio_path: Path | None = None
    duration: float = 0.0
    image_path: Path | None = None
    image_credit: str = ""
    image_source: str = ""
    image_license: str = ""             # 版权标注（如 CC0 公共领域）
    slide_path: Path | None = None      # 成片画面（合成层）
    segment_path: Path | None = None
    error: str = ""

    @property
    def chars(self) -> int:
        return len(self.text or "")


@dataclass
class Chapter:
    index: int
    heading: str                   # 章节标题（画面大字）
    summary: str = ""              # 本章要讲什么（写稿用，不进画面）
    seconds: int = 60              # 目标时长
    facts: list[str] = field(default_factory=list)   # 本章必须依托的可考史实锚点
    image_queries: list[str] = field(default_factory=list)
    image_queries_en: list[str] = field(default_factory=list)   # 给博物馆英文索引用
    scenes: list[Scene] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.scenes)

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.scenes)


@dataclass
class Story:
    topic: str
    title: str = ""
    angle_question: str = ""       # 本期要回答的那个具体问题（小切口模式）
    hook: str = ""                 # 开篇钩子（片头口播用）
    period: str = ""               # 「东汉末年 208 年」这类表述
    period_start: int = 0          # 年代区间（公元前为负），用于年份合理性校验
    period_end: int = 0
    chapters: list[Chapter] = field(default_factory=list)
    material: str = ""             # 检索到的参考素材（溯源用）
    notes: list[str] = field(default_factory=list)

    @property
    def all_scenes(self) -> list[Scene]:
        out: list[Scene] = []
        for ch in self.chapters:
            out.extend(ch.scenes)
        return out

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.all_scenes)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.all_scenes)
