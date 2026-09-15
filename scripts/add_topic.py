"""制定选题条目（两级：故事类型 + 标题与描述），不用改 Python。

为什么要有这个工具：选题池原来是写在 `topics.py` 里的常量，要加一条得改代码；
而「栏目要做什么题」是**你的**判断，不该需要改代码才能表达。

产物：`data/topics_user.json`（config: `story.user_topics`）
它会被并进选题池 —— `run.bat topics` 能看到，自动选题也会挑到，
`--type` 过滤、避开最近做过的类型、查重一样生效。

用法：
    :: 看自己写了什么
    python scripts\\add_topic.py --list

    :: 加一条（类型用内置的 15 类之一）
    python scripts\\add_topic.py --type "刑狱与流放" ^
        --title "午门之外：一场秋决是怎么走完的？" ^
        --desc "从判决到行刑的流程与其中的人：谁复核、谁监斩、犯人最后一夜怎么过。"

    :: 自己新开一个类型（带上类型的高层描述）
    python scripts\\add_topic.py --type-desc "近代交通与物流：铁路、轮船、电报如何改变距离感。" ^
        --type "近代交通" --title "京张铁路：一条路修了几年、花了多少？" ^
        --desc "从勘测到通车的账：用了多少钱、多少工、通了以后沿线变了什么。"

    :: 删掉一条 / 体检
    python scripts\\add_topic.py --remove "午门之外：一场秋决是怎么走完的？"
    python scripts\\add_topic.py --check

约定（写之前先看这三条）：
  1. **标题**用「小切口：具体疑问」的形式，15-30 字；事件式则点出事件与看点。
  2. **描述**写「这一期到底讲什么、要回答什么」，35-60 字。
     ⚠️ 不要在描述里写具体数字、年代或结论 —— 那些要由写稿阶段的史实校验兜住，
     写进选题池就等于把一个未核实的说法固化进长期资产，错了会一直传下去。
  3. **类型**是受控词表：要么用内置的，要么用 `--type-desc` 新开一个并写清它讲什么。
     类型不对，「避开连着做同一路」这类功能就悄悄失效了。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hsg import topics as T                     # noqa: E402
from hsg.config import load_config              # noqa: E402


def _pool(a) -> T.UserPool:
    path = Path(a.file) if a.file else T.user_pool_path(load_config())
    return T.load_user_pool(path)


def do_list(pool: T.UserPool) -> int:
    print(f"\n自己制定的选题：{len(pool.topics)} 条　自定义类型：{len(pool.types)} 个")
    print(f"文件：{pool.path}\n")
    if not pool.topics:
        print("（还是空的。加一条：python scripts\\add_topic.py --type \"刑狱与流放\" "
              "--title \"…\" --desc \"…\"）")
        return 0
    eps = pool.episodes()
    if eps:
        print(f"（其中系列条目 {len(eps)} 集，用 run.bat series 看进度）")
    by_type: dict[str, list[T.Topic]] = {}
    for t in pool.topics:
        by_type.setdefault(t.type or T.UNKNOWN_TYPE, []).append(t)
    merged = pool.merged_types()
    for name, items in by_type.items():
        print(f"■ {name}　{merged.get(name, '（未定义描述）')}")
        for t in items:
            print(f"    · [{getattr(t, 'mode', 'small')}] {t.title}")
            print(f"      {t.desc or '（没有描述 —— 出稿时会由模型生成）'}")
        print()
    return 0


def do_add(a, pool: T.UserPool) -> int:
    merged = pool.merged_types()
    ttype = str(a.type or "").strip()
    if not ttype:
        print("✗ 必须给 --type（用 --list 或 run.bat topics 看有哪些类型）")
        return 2
    if a.type_desc:
        # 声明/更新一个类型
        pool.types[ttype] = str(a.type_desc).strip()
        merged[ttype] = str(a.type_desc).strip()
        print(f"✓ 类型「{ttype}」已写入（描述 {len(str(a.type_desc).strip())} 字）")
    elif ttype not in merged:
        print(f"✗ 没有这个类型：{ttype}")
        print(f"  内置类型：{'、'.join(T.all_types())}")
        print("  要新开一个类型，请连描述一起给：--type-desc \"这一类故事讲什么\"")
        return 2
    title = str(a.title or "").strip()
    desc = str(a.desc or "").strip()
    mode = str(a.mode or "small")
    series = str(getattr(a, "series", "") or "").strip()
    ep = int(getattr(a, "ep", 0) or 0)
    if series:
        if ep <= 0:
            print("✗ 系列片必须给 --ep（集号，1 起）")
            return 2
        if getattr(a, "series_desc", None):
            pool.series[series] = str(a.series_desc).strip()
            print(f"✓ 系列《{series}》已记录（说明 {len(str(a.series_desc).strip())} 字）")
        elif series not in pool.series:
            pool.series[series] = ""
            print(f"⚠ 《{series}》还没有系列说明，补上更好：--series-desc \"这个系列讲什么\"")
        same = [t for t in pool.episodes(series) if t.ep == ep]
        if same:
            print(f"✗ 第 {ep} 集已经有了：{same[0].title}")
            return 2
    if not title:
        print("✗ 必须给 --title")
        return 2
    if len(title) < 8:
        print(f"✗ 标题太短（{len(title)} 字）：选题要能看出切口，建议 15-30 字")
        return 2
    dup = T.topic_of(title, pool)
    if dup.type:      # topic_of 只在池子里找到才带类型
        print(f"✗ 这条标题池子里已经有了（类型 {dup.type}）")
        return 2
    if len(desc) < 15:
        print(f"⚠ 描述只有 {len(desc)} 字，偏短（建议 35-60 字）。"
              "留空也行 —— 出稿时会由模型生成")
    pool.topics.append(T.Topic(type=ttype, title=title, desc=desc, mode=mode,
                               series=series, ep=ep))
    q = T.save_user_pool(pool)
    where = f"《{series}》第 {ep} 集" if series else f"{ttype} · {mode}"
    print(f"✓ 已加入（{where}）：{title}")
    print(f"  文件：{q}")
    if series:
        print(f"  看进度：run.bat series \"{series}\"　　出稿：run.bat agent --stage 1 --series \"{series}\"")
    else:
        print("  看效果：run.bat topics　　直接跑：run.bat -t \"" + title + "\"")
    return 0


def do_remove(a, pool: T.UserPool) -> int:
    want = str(a.remove or "").strip()
    keep = [t for t in pool.topics if t.title != want]
    if len(keep) == len(pool.topics):
        hit = [t for t in pool.topics if want in t.title]
        if len(hit) == 1:
            keep = [t for t in pool.topics if t.title != hit[0].title]
            want = hit[0].title
        else:
            print(f"✗ 没找到（或匹配到 {len(hit)} 条）：{want}")
            return 2
    pool.topics = keep
    T.save_user_pool(pool)
    print(f"✓ 已删除：{want}")
    return 0


def do_check(pool: T.UserPool) -> int:
    """体检：类型是否都有描述、标题是否重复、描述是否踩了「别写数字」那条。"""
    import re

    merged = pool.merged_types()
    bad = 0
    for t in pool.topics:
        if t.type not in merged:
            print(f"✗ {t.title}\n    类型「{t.type}」不在词表里")
            bad += 1
        elif not merged.get(t.type):
            print(f"✗ {t.title}\n    类型「{t.type}」没有高层描述")
            bad += 1
        if not t.desc:
            print(f"⚠ {t.title}\n    没有描述（出稿时会由模型生成，能跑但不如自己写准）")
        elif re.search(r"\d", t.desc):
            print(f"⚠ {t.title}\n    描述里有数字（{t.desc[:24]}…）—— 描述里的数字会被当成"
                  "既成事实，别在这里下结论，交给写稿阶段的史实校验")
        if str(getattr(t, "mode", "small")) not in ("small", "event"):
            print(f"✗ {t.title}\n    mode 只能是 small / event，现在是 {getattr(t, 'mode', '')!r}")
            bad += 1
    titles = [t.title for t in pool.topics] + T.TOPICS_ANGLE + T.TOPICS_EVENT
    seen = {x for x in titles if titles.count(x) > 1}
    for x in seen:
        print(f"✗ 标题重复：{x}")
        bad += 1
    print(f"\n{'✓ 全部合格' if not bad else f'✗ {bad} 条要改'}（共 {len(pool.topics)} 条自己写的选题）")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="制定选题条目（两级：类型 + 标题与描述）")
    ap.add_argument("--file", help="写到别的文件（默认 config 的 story.user_topics）")
    ap.add_argument("--type", help="L1 故事类型（内置 15 类之一，或配合 --type-desc 新开）")
    ap.add_argument("--type-desc", dest="type_desc", help="新开/更新一个类型的「这一类讲什么」")
    ap.add_argument("--title", help="L2 标题（15-30 字，小切口：具体疑问）")
    ap.add_argument("--desc", help="L2 描述（35-60 字，这一期到底讲什么；别写数字与结论）")
    ap.add_argument("--mode", choices=["small", "event"], help="切入方式（默认 small）")
    ap.add_argument("--series", help="系列名（如「古代十大权臣」）")
    ap.add_argument("--series-desc", dest="series_desc", help="这个系列讲什么（建第一集时给一次）")
    ap.add_argument("--ep", type=int, help="系列集号（1 起；系列片必须给）")
    ap.add_argument("--list", action="store_true", help="列出自己写的选题")
    ap.add_argument("--remove", help="按标题删除一条")
    ap.add_argument("--check", action="store_true", help="体检自己写的选题")
    a = ap.parse_args()

    pool = _pool(a)
    if a.list:
        return do_list(pool)
    if a.check:
        return do_check(pool)
    if a.remove:
        return do_remove(a, pool)
    if a.title or a.type_desc:
        return do_add(a, pool)
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
