"""检索评测：合成语料 + 固定用例，对比 keyword / vector / hybrid 三路。

语料按个人记忆库的特征建模（中文短条目、小节前缀、含"的"虚词噪声、
领域干扰项、短身份行、跨语言查询），不包含任何真实个人信息，任何
机器装好依赖后可复现同样数字。

控制变量约定：语料规模、用例内容、top-k、模型与指标定义全部固定在本脚本里，
main 与 evolution 必须运行同一份脚本（同一解释器、同一本地 embedding 模型），
只切换被测分支的代码。合成语料规模按真实库有效事实的 80% 以上设定
（当前真实库 active=886 / total=906，阈值 709 / 725；本脚本生成 740 条）。

用法：
    uv run python evals/retrieval_eval.py            # 全部三路（需 local-embed extra）
    uv run python evals/retrieval_eval.py --keyword  # 只跑 BM25，离线可用
"""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from syvrio.infra.config import Config, LLMConfig, RetrievalConfig
from syvrio.memory.service import MemoryService
from syvrio.retrieval import KeywordRetriever
from syvrio.retrieval.vector import LocalEmbedder, VectorRetriever

# 每个领域生成的干扰项数量；7 个领域共 700 条，加 40 条目标 = 740 条（≥725）。
DISTRACTORS_PER_DOMAIN = 100

# ---------------------------------------------------------------------------
# 合成语料：目标是条目 + 各领域"的"字噪声干扰项
# ---------------------------------------------------------------------------

# (topic, 条目文本) —— 用例的命中目标；人物、数值、偏好均为虚构占位
TARGETS = [
    # 咖啡与饮食
    ("food", "咖啡器具包括法压壶、分享壶和电子秤。"),
    ("food", "喜欢坚果调、醇厚度高的咖啡风味，不喜欢过酸的浅烘。"),
    ("food", "手冲记录：15g 粉、240ml 水、92℃、2 分半，最后摇晃分享壶再出杯。"),
    ("food", "连锁咖啡主要点美式，不喝加糖浆的风味拿铁。"),
    ("food", "基本不喝奶茶，默认少糖；太甜的饮品会觉得腻。"),
    ("food", "周末喜欢自己磨豆子，用的是一台手摇磨豆机。"),
    ("food", "早餐常吃燕麦片配无糖酸奶，图方便。"),
    # 居住生活
    ("housing", "住在城南的一处合租房，合同按季度续签。"),
    ("housing", "租房要求有阳台和电梯，通勤控制在 40 分钟内。"),
    ("housing", "玄关放了收纳盒，钥匙和门禁卡固定放一起。"),
    ("housing", "合租室友养了一只猫，公共区域每周轮流打扫。"),
    ("housing", "房间朝向南，下午采光好，夏天偏热。"),
    # 日常工作
    ("career", "工作内容以整理运营周报和跟进活动排期为主。"),
    ("career", "本月参加了两场行业分享会，主题是增长分析。"),
    ("career", "面试复盘发现对数据透视表还不熟，准备补一补。"),
    ("career", "常用的协作工具是在线文档和看板，周会固定在周一。"),
    ("career", "最近在跟进一场线下活动的物料清单，截止时间提前了一周。"),
    # 课程与阅读
    ("learning", "正在学习摄影课程，期末作品选城市夜景。"),
    ("learning", "本周完成了两章读书笔记，还差一章没整理。"),
    ("learning", "报名了线上的公开课程，每周三晚上直播。"),
    ("learning", "正在背英语单词，每天打卡三十个，用碎片时间复习。"),
    ("learning", "摄影课的作业要求交一组黑白照片，主题自选。"),
    # 身份（独立短字段，触发字段精确匹配；人物为占位角色）
    ("identity", "姓名：人物甲"),
    ("identity", "年龄：30 岁"),
    ("identity", "家乡：某省某市某镇"),
    ("identity", "职业：运营助理"),
    ("identity", "专业：市场营销"),
    # 健康与作息
    ("health", "身高 180cm、体重 70kg，均为自测的虚构数据。"),
    ("health", "年度体检已预约，报告可以线上查看。"),
    ("health", "对花粉轻微过敏，春季出门会戴口罩。"),
    ("health", "每周跑步三次，每次五公里，配速大约六分半。"),
    ("schedule", "作息比较固定：早 7 点半起床，晚 12 点睡，周末会晚一些。"),
    ("schedule", "工作日午休一小时，通常趴在工位上休息。"),
    ("schedule", "周六上午固定去菜市场，周日睡到自然醒。"),
    # 设备
    ("devices", "键盘是 87 键的办公款，红轴。"),
    ("devices", "耳机是半入耳式蓝牙款，主要用于视频会议。"),
    ("devices", "显示器是 27 寸的 4K 屏，外接笔记本使用。"),
    ("devices", "手机用的是安卓机，开了深色模式护眼。"),
    # 文娱
    ("entertainment", "闲下来喜欢看悬疑剧，不喜欢节奏太慢的文艺片。"),
    ("entertainment", "歌单里循环最多的是轻音乐和爵士，工作时当背景音。"),
]

# 领域干扰项模板：含"的"与领域词，但与任何用例无关——模拟真实库的虚词噪声
_DISTRACTORS = {
    "food": [
        "当时的{place}的咖啡店的会员卡已经找不到了。",
        "同事推荐的{place}的甜品据说不错，还没有去过。",
        "公司楼下的便利店的{thing}经常缺货。",
    ],
    "housing": [
        "搬家的时候的{thing}的处理方式还没有想好。",
        "中介发来的房子的照片看起来不太真实。",
        "小区的物业的通知经常看漏。",
    ],
    "career": [
        "周报里的数据的口径又统一了一次。",
        "会议室的投影的转接头经常找不到。",
        "活动群里的接龙的截止时间总被看错。",
    ],
    "learning": [
        "课程群里的raw文件的命名很乱。",
        "三脚架的收纳的包总是塞不平。",
        "夜景的噪点的处理教程收藏了还没看。",
    ],
    "identity": [
        "证件照的重拍的截止时间在月底。",
        "表格里的信息的核对花了一个下午。",
    ],
    "health": [
        "体检的报告的纸质版还没有去取。",
        "药房的营业的时间与下班的路程顺路。",
    ],
    "devices": [
        "桌面的理线的方案改了两次还是不满意。",
        "充电器的大部分的接口都是旧的规格。",
    ],
}
_PLACE = ["公司旁边", "家附近", "地铁站里"]
_THING = ["储物柜", "快递柜", "置物架"]


def build_corpus() -> list[tuple[str, str]]:
    """确定性生成 740 条语料：40 条目标 + 每领域 100 条干扰（共 700）。"""
    entries = list(TARGETS)
    for domain, templates in _DISTRACTORS.items():
        count = 0
        round_index = 0
        while count < DISTRACTORS_PER_DOMAIN:
            for template in templates:
                text = template.format(
                    place=_PLACE[(count + round_index) % len(_PLACE)],
                    thing=_THING[(count + round_index) % len(_THING)],
                ) + f"（记录 {count}）"
                entries.append((domain, text))
                count += 1
                if count >= DISTRACTORS_PER_DOMAIN:
                    break
            round_index += 1
    return entries


# ---------------------------------------------------------------------------
# 评测用例：(类别, 查询, 可接受子串列表；top-k 内命中任一即算相关)
# 固定 50 例；main 与 evolution 使用完全相同的题面与可接受子串。
# ---------------------------------------------------------------------------

CASES = [
    # 原词（13）
    ("原词", "法压壶", ["法压壶"]),
    ("原词", "电子秤", ["电子秤"]),
    ("原词", "收纳盒", ["收纳盒"]),
    ("原词", "行业分享会", ["行业分享会"]),
    ("原词", "期末作品", ["期末作品"]),
    ("原词", "红轴 键盘", ["红轴"]),
    ("原词", "数据透视表", ["数据透视表"]),
    ("原词", "年度体检", ["年度体检"]),
    ("原词", "磨豆机", ["磨豆机"]),
    ("原词", "燕麦片", ["燕麦片"]),
    ("原词", "看板", ["看板"]),
    ("原词", "黑白照片", ["黑白照片"]),
    ("原词", "4K 屏", ["4K"]),
    # 口语改写（15）
    ("改写", "我喝咖啡喜欢什么口味", ["坚果", "醇厚"]),
    ("改写", "我住的地方有什么要求", ["阳台", "电梯"]),
    ("改写", "我早上一般几点起床", ["早 7 点半", "作息"]),
    ("改写", "我最近在复习什么", ["数据透视表", "面试复盘"]),
    ("改写", "我的期末作品进展", ["期末作品", "城市夜景"]),
    ("改写", "我用什么键盘", ["键盘"]),
    ("改写", "我身体有什么数据记录", ["身高", "体重"]),
    ("改写", "我参加过哪些行业活动", ["行业分享会", "增长分析"]),
    ("改写", "我不喜欢喝什么", ["奶茶", "糖浆"]),
    ("改写", "我早餐一般吃什么", ["燕麦片", "酸奶"]),
    ("改写", "我室友养了什么宠物", ["猫"]),
    ("改写", "我房间采光怎么样", ["采光", "朝向南"]),
    ("改写", "我周会固定在什么时候", ["周一"]),
    ("改写", "我在背什么单词", ["英语单词", "打卡"]),
    ("改写", "我跑步的频率和配速", ["跑步", "六分半"]),
    # 身份（6）
    ("身份", "我是谁", ["姓名：人物甲"]),
    ("身份", "我叫什么名字", ["姓名"]),
    ("身份", "我多大了", ["30 岁"]),
    ("身份", "我的家乡在哪里", ["家乡"]),
    ("身份", "我的职业是什么", ["运营助理"]),
    ("身份", "我学什么专业", ["市场营销"]),
    # 数字（7）
    ("数字", "我身高多少", ["180"]),
    ("数字", "手冲咖啡的粉水参数", ["15g", "240ml"]),
    ("数字", "通勤时间控制在多久", ["40 分钟"]),
    ("数字", "每周几晚上直播课", ["周三"]),
    ("数字", "我显示器多大尺寸", ["27 寸"]),
    ("数字", "我每天背多少个单词", ["三十个"]),
    ("数字", "我每周跑几次步", ["三次"]),
    # 跨语言（5）
    ("跨语言", "coffee flavor preference", ["坚果", "咖啡"]),
    ("跨语言", "my hometown", ["家乡"]),
    ("跨语言", "my monitor size", ["27 寸", "显示器"]),
    ("跨语言", "what music do I listen to", ["轻音乐", "爵士"]),
    ("跨语言", "my weekly running routine", ["跑步", "五公里"]),
    # 偏好与否定（4）
    ("偏好", "我喜欢看什么类型的剧", ["悬疑剧"]),
    ("偏好", "我工作时听什么音乐", ["轻音乐", "爵士"]),
    ("偏好", "我对什么过敏", ["花粉"]),
    ("偏好", "我手机开了什么模式", ["深色模式"]),
]


def evaluate(store, retrievers: dict) -> dict:
    results: dict[str, dict] = {}
    for name, retriever in retrievers.items():
        p3 = p5 = 0
        mrr = 0.0
        per_category: dict[str, list[bool]] = {}
        failures = []
        for category, query, subs in CASES:
            hits = retriever.search(query, 10)
            rank = next(
                (i + 1 for i, hit in enumerate(hits) if any(s in hit.text for s in subs)),
                None,
            )
            ok3 = rank is not None and rank <= 3
            ok5 = rank is not None and rank <= 5
            p3 += ok3
            p5 += ok5
            mrr += 1.0 / rank if rank else 0.0
            per_category.setdefault(category, []).append(ok3)
            if not ok3:
                top = hits[0].text[:36].replace("\n", " ") if hits else "无结果"
                failures.append(f"  [{category}] {query} → {top}")
        results[name] = {
            "p3": p3 / len(CASES),
            "p5": p5 / len(CASES),
            "mrr": mrr / len(CASES),
            "per_category": per_category,
            "failures": failures,
        }
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="检索评测（合成语料，脱敏，控制变量）")
    parser.add_argument("--keyword", action="store_true", help="只跑 BM25（离线）")
    parser.add_argument("--failures", action="store_true", help="打印未过 @3 的用例")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="syvrio-eval-") as tmp:
        vault = Path(tmp) / "vault"
        config = Config(vault=vault, llm=LLMConfig(), retrieval=RetrievalConfig(method="keyword"))
        service = MemoryService(config)
        service.init_vault()
        for topic, text in build_corpus():
            service.store.append(topic, text, source="eval", confidence=1.0)
        print(f"语料 {len(service.store.entries())} 条 · 用例 {len(CASES)} 例\n")

        retrievers: dict = {"keyword": KeywordRetriever(service.store)}
        if not args.keyword:
            if not LocalEmbedder.available():
                raise SystemExit("未安装 local-embed extra，先用 --keyword 或 uv sync --extra local-embed")
            embedder = LocalEmbedder("BAAI/bge-small-zh-v1.5")
            vector = VectorRetriever(service.store, embedder)
            retrievers["vector"] = vector
            retrievers["hybrid"] = _hybrid(service, vector)

        results = evaluate(service.store, retrievers)
        print(f"{'':<10}{'Recall@3':>10}{'Recall@5':>10}{'MRR@10':>9}")
        for name, r in results.items():
            print(f"{name:<10}{r['p3']:>9.0%}{r['p5']:>10.0%}{r['mrr']:>9.3f}")
        print("\n分类 Recall@3：")
        print(f"{'':<8}" + "".join(f"{name:>10}" for name in results))
        categories = [c for c, _, _ in CASES]
        for category in dict.fromkeys(categories):
            line = f"{category:<8}"
            for name, r in results.items():
                rows = r["per_category"][category]
                line += f"{sum(rows) / len(rows):>10.0%}"
            print(line)
        if args.failures:
            for name, r in results.items():
                if r["failures"]:
                    print(f"\n{name} 未过 @3：")
                    print("\n".join(r["failures"]))


def _hybrid(service: MemoryService, vector: VectorRetriever):
    from syvrio.retrieval import HybridRetriever

    return HybridRetriever(KeywordRetriever(service.store), vector)


if __name__ == "__main__":
    main()
