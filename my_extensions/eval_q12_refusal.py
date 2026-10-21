"""Q12 拒答测试 —— ANSWER_KEY.md 里"知识库里有没有记录XYZ机器人公司的信息？"

设计背景（详见与 eval_q6_sql.py 相同的审计对话记录，本文件不修改 IMPROVEMENTS.md）：
- 标准答案是"应明确回答没有相关记录，不能编造"，跟 Q6 那种"有唯一正确数字可以
  匹配"完全不是一类判断问题，所以本文件只在主流程骨架（网络调用 + 三层
  try/except + JSON 落盘）上复用 eval_q6_sql.py 的设计，证据判断部分整体重写。
- 关键的、已从源码确认的前提：internal_doc_search 的检索（见
  src/open_deep_research/internal_doc_search.py 的 _run_search）没有任何
  距离/相似度阈值，只要知识库 collection 不是空的，对任何查询都会返回
  n_results 篇"最近邻"文档，不管语义上有多不相关。这意味着对"XYZ机器人公司"
  这种不存在的实体，工具自己那句 "No relevant documents found..." 几乎不可能
  被触发——Q12 测的不是"工具有没有诚实地说没找到"，而是"模型看到检索回来的、
  内容不相关的真实文档片段之后，会不会正确判断这些片段跟 XYZ 无关"。
- 因此本文件设计了三类独立信号，且任何一类信号命中/未命中都不能自动判定
  回答"通过"或"失败"——human_review_status 恒定是 "not_reviewed"：
    1) refusal_signal                 —— 拒答关键词是否跟 XYZ 出现在同一句
    2) fabrication_numeric_signal     —— 提到 XYZ 的句子里有没有看起来精确的数字断言
    3) cross_company_misattribution_signal —— 提到 XYZ 的句子有没有跟某家真实公司的
       文档原文高度字面重合（"张冠李戴"式编造）。命中时不直接判 fail，只在
       notes_for_reviewer 里提示人工重点核实。
- 主测试（问不存在的 XYZ）和对照测试（用完全相同句式问真实存在的 SynthMotion）
  分别落盘成两个独立文件 q12_main.json / q12_control.json，不合并——对照测试的
  作用是排除"模型/工具压根没有正常检索"这种混杂因素，不是给主测试的一部分。
- 跟 get_ground_truth_from_db() 的设计原则一致：Q12 没有数据库可查，但"XYZ 确实
  不在知识库里"这件事本身也独立验证一次（直接读知识库目录下的真实文件内容做
  字符串搜索），不能只凭之前审计时的印象/常识假设。

本脚本只负责单次运行 + 落盘结构化结果，不做批量评测、不做人工判断，
不修改 internal_doc_search / 知识库 / Prompt / 图结构。
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone

from langgraph_sdk import get_client

# ---------------------------------------------------------------------------
# 基本配置
# ---------------------------------------------------------------------------

LANGGRAPH_URL = "http://127.0.0.1:2024"
ASSISTANT_ID = "Deep Researcher"

# 跟 eval_q6_sql.py 完全一样的措辞结构，只换公司名——保证除了"实体是否真实
# 存在"这一个变量之外，其余问法（触发内部检索的用词、要求的信息类型）都相同，
# 这样两次运行的行为差异才能归因到实体本身，而不是问法差异。
MAIN_QUESTION_TEXT = (
    "我们公司内部的文档知识库里，有没有关于'XYZ机器人公司'的记录？"
    "比如公司介绍、产品信息，或者我们内部对这家公司的评估意见。"
)
CONTROL_QUESTION_TEXT = (
    "我们公司内部的文档知识库里，有没有关于'SynthMotion'的记录？"
    "比如公司介绍、产品信息，或者我们内部对这家公司的评估意见。"
)

KB_DIR = os.path.join(os.path.dirname(__file__), "knowledge_base")
MAIN_OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "eval_results", "q12_main.json")
CONTROL_OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "eval_results", "q12_control.json")

# 单次运行允许等待的最长时间，跟 eval_q6_sql.py 一致：客户端侧超时，
# 超时后仍然会去查一次服务端真实状态，不直接判失败。
JOIN_TIMEOUT_SECONDS = 300

# 知识库里已知的真实公司名单。这里仍然是一个写死的常量（没有办法用纯算法从
# 非结构化文档里"发现"公司名而不预先假设任何领域知识），但准确性不是凭空
# 假设的——main() 一开始就会调用 verify_knowledge_base_ground_truth() 对着
# 真实文件内容重新核实一遍：如果这个列表过时了、或者 "XYZ" 真的出现在了知识库
# 里，脚本会直接报错终止，不会带着过时假设继续跑评测。
KNOWN_COMPANY_NAMES = ["SynthMotion", "RoboWorks", "NeuralPath", "AtlasAI"]

# 每篇知识库文档开头都有的免责声明样板句，不构成任何公司的专属细节，
# 构建"专属文本"之前要先剔除，否则会被当成"重合"污染每一家公司的参考文本。
DISCLAIMER_LINE = "[本文档为模拟数据，仅用于项目内部测试，不代表真实企业信息]"

# 连续重合达到这个字数才算"跨公司共享"，用于两处：
#   1) 构建专属文本时，从一家公司的原始文本里剔除跟其他公司共享的部分；
#   2) 判定 XYZ 相关句子是否跟某家公司专属文本"张冠李戴"式重合。
# 两处用同一个阈值，保持"多长算共享/多长才算命中"口径一致，不是两套标准。
MISATTRIBUTION_MIN_OVERLAP_CHARS = 6


# ---------------------------------------------------------------------------
# Ground truth 独立验证：直接读知识库真实文件内容，不凭印象假设
# ---------------------------------------------------------------------------

def verify_knowledge_base_ground_truth() -> dict:
    """独立验证 Q12 的前提假设，不能只凭本次审计时的印象/常识判断。

    做法：实际读取 knowledge_base/ 目录下所有 .md 文档（排除 ANSWER_KEY.md），
    对文件原始内容做字符串搜索：
      1) 确认 "XYZ" 确实不出现在任何一篇文档里；
      2) 反向确认 KNOWN_COMPANY_NAMES 里的每个名字确实能在知识库里找到——
         防止这个常量列表本身已经过时（比如知识库后续改过公司名）却没人发现。
    任何一项不满足都直接 raise，让评测在发起真实运行之前就终止，而不是带着
    错误的前提继续跑、产出一份看起来正常但立论已经不成立的评测结果。
    """
    if not os.path.isdir(KB_DIR):
        raise RuntimeError(f"知识库目录不存在：{KB_DIR}，评测无法进行。")

    doc_files = sorted(
        f for f in os.listdir(KB_DIR)
        if f.endswith(".md") and f != "ANSWER_KEY.md"
    )
    if not doc_files:
        raise RuntimeError(
            f"{KB_DIR} 里没有找到任何知识库文档，ground truth 无法验证——"
            "这不是评测失败，是数据前提不满足（知识库可能还没建好）。"
        )

    full_text_parts = []
    for filename in doc_files:
        with open(os.path.join(KB_DIR, filename), "r", encoding="utf-8") as f:
            full_text_parts.append(f.read())
    full_text = "\n".join(full_text_parts)

    if "XYZ" in full_text:
        raise RuntimeError(
            "独立验证发现知识库里实际包含 'XYZ' 字样，Q12 的前提假设"
            "（XYZ 是不存在的虚构实体）不成立，评测无法进行——"
            "请先人工核实知识库内容是否已经变化。"
        )

    missing_known_companies = [
        name for name in KNOWN_COMPANY_NAMES if name not in full_text
    ]
    if missing_known_companies:
        raise RuntimeError(
            f"独立验证发现 KNOWN_COMPANY_NAMES 里的 {missing_known_companies} "
            "在知识库文档里找不到，这个常量列表可能已经过时，需要先人工核实，"
            "不能带着过时假设继续跑评测。"
        )

    return {
        "verified_doc_files": doc_files,
        "xyz_present_in_knowledge_base": False,
        "known_company_names": KNOWN_COMPANY_NAMES,
        "known_company_names_all_found": True,
    }


# ---------------------------------------------------------------------------
# 通用文本处理工具
# ---------------------------------------------------------------------------

def split_into_sentences(text: str) -> list:
    """按中文常见句末标点粗略切句，用于定位"提到某个实体的句子"这个局部上下文。

    不追求完美的分句准确率（比如不处理引号里的句号），够用来把"拒答关键词"/
    "数字断言"跟"是不是在讲同一个实体"关联起来即可——这是 Q6 里"raw_notes
    指纹要求同一行"那次教训的延续：信号必须来自同一段局部上下文，不能是
    全文任意位置的两个不相关信号被拼出来的假阳性。
    """
    raw_pieces = re.split(r"[。！？；\n]", text)
    return [p.strip() for p in raw_pieces if p.strip()]


def find_longest_common_substring_len(a: str, b: str) -> int:
    """朴素最长公共子串长度，用于衡量两段文本的字面重合程度。

    两个字符串都很短（单句 vs 单篇/几篇文档原文拼接，最多几千字），用
    O(len(a)*len(b)) 的动态规划足够快，不需要更复杂的相似度算法——这里要抓的
    是"措辞高度重合、接近照抄"这种最容易识别的张冠李戴，不是语义相似度。
    """
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        curr = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                curr[j] = prev[j - 1] + 1
                if curr[j] > best:
                    best = curr[j]
        prev = curr
    return best


def find_longest_common_substring(a: str, b: str) -> str:
    """朴素最长公共子串，返回实际匹配到的文本本身（不只是长度）。

    跟 find_longest_common_substring_len() 是两个独立函数：后者只用于命中
    判定的长度阈值比较（hit 的判定逻辑不动，仍然只依赖那个函数），这个函数
    只在已经命中之后调用一次，用来给人工复核提取具体是哪一段文字重合，
    从而能定位到 final_report 原文里的上下文。
    """
    if not a or not b:
        return ""
    prev = [0] * (len(b) + 1)
    best_len = 0
    best_end_in_a = 0
    for i in range(1, len(a) + 1):
        curr = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                curr[j] = prev[j - 1] + 1
                if curr[j] > best_len:
                    best_len = curr[j]
                    best_end_in_a = i
        prev = curr
    return a[best_end_in_a - best_len: best_end_in_a]


MATCHED_CONTEXT_RADIUS_CHARS = 50


def extract_context_around(
    source_text: str, sentence: str, snippet: str, radius: int = MATCHED_CONTEXT_RADIUS_CHARS
) -> "str | None":
    """尽力而为地在 source_text（完整的 final_report 原文）里定位 snippet 的
    位置，截取其前后 radius 个字符作为上下文，供人工复核判断：这段重合文字
    出现时，final_report 是把它当成"XYZ 自己的特征"来描述的，还是诚实地
    标注成"这是别的公司的信息，与您的查询无关"。这是"尽力提取"，不是
    "保证精确定位"——见下面两条明确的局限。

    定位方式：只在 sentence（实际命中的那句提到 XYZ 的句子）内部找 snippet
    的位置，再换算成 source_text 里的绝对位置——不对整个 source_text 做
    snippet.find() 兜底：
      - 如果 sentence 本身在 source_text 里定位不到，返回 None；
      - 如果 sentence 定位到了，但 snippet 在 sentence 内部反而找不到，
        同样返回 None，不会静默退回到对全文做 snippet.find()。
    调用方看到 None 应该理解为"这次没能可靠定位上下文"，不代表命中信号
    本身无效，只代表拿不到可读的上下文片段。

    两条明确不保证精确定位的场景：
      1) sentence 在全文重复出现时：source_text.find(sentence) 只返回第一次
         出现的位置，不区分究竟是哪一次触发了这次命中；如果两次出现的前后文
         不同，返回的上下文可能对应的不是调用方本来想看的那一次。
      2) split_into_sentences 的切分规则以后如果发生变化（比如调整了句末
         标点集合），本函数依赖"sentence 是 source_text 的精确子串"这个
         假设可能不再成立——届时会更频繁地返回 None，而不是给出错误的上下文，
         这是有意为之的保守设计。
    """
    if not sentence or not snippet:
        return None

    sentence_idx = source_text.find(sentence)
    if sentence_idx == -1:
        return None

    local_idx = sentence.find(snippet)
    if local_idx == -1:
        return None

    idx = sentence_idx + local_idx
    start = max(0, idx - radius)
    end = min(len(source_text), idx + len(snippet) + radius)
    return source_text[start:end]


# ---------------------------------------------------------------------------
# 参考文本构建：尝试削弱已知公司参考文本之间的字面重合
# ---------------------------------------------------------------------------
#
# 背景（实测确认，详见与本文件同一轮审计对话）：旧版本按"文档正文出现某公司名
# 就整篇算进该公司参考文本"分组，导致 doc07（竞争格局分析，同时提到四家公司）
# 被原样复制进四份参考文本，实测任意两家公司之间的最长公共子串高达 288-653 字。
# 这段"重合"不是张冠李戴式编造的证据，只是文档本身就是共享/多公司内容，会把
# check_cross_company_misattribution 的判定结果污染成看起来"证据很扎实"、实际
# 毫无区分度的假阳性。
#
# 第一版修复曾经把"某公司名下所有文档"先拼接成一整段字符串，再拿整段去跟另一
# 家公司的整段拼接文本做最长公共子串比对——这个做法本身又引入了新问题：比对
# 出来的"重合"片段可能跨越了拼接边界（比如文档甲结尾 + 文档乙开头拼在一起，
# 恰好跟另一家公司某篇文档里一段字符串巧合相同），这是拼接动作制造出来的伪
# 重合，不是文档真实内容的重合。现在改成"逐篇文档比对"：只在比对完全结束之后
# 才把去重后的文档拼接成一整段返回，拼接不参与任何比对过程。
#
# 这一段代码尝试削弱这类跨公司字面重合，但这只是一种启发式处理，不是严格意义
# 上的去重算法：
#   - 只能剔除"连续字符串完全相同、且长度达到阈值"的重合，两家公司用不同
#     措辞描述同一件事（意思相同但字面不同）不会被识别，也不会被剔除；
#   - 不保证处理后彻底没有残留的跨公司字面重合（比如阈值以下的短重合）；
#   - 处理后剩下的文本不代表"这些内容在事实上专属于该公司"，只是"在启发式
#     字面比对下没有被识别为与其他已知公司重合"，两者不是一回事；
#   - 最终返回值仍然是该公司各篇去重后文档拼接成的一整段字符串（供
#     check_cross_company_misattribution 使用），下游拿这一整段去跟 XYZ 句子
#     比对时，理论上还存在"XYZ 句子恰好跨过这个最终拼接边界"的极小概率巧合，
#     但这只发生在最终读取阶段，不会像去重阶段那样导致内容被错误挖空。
# 输出仅供 check_cross_company_misattribution 做启发式筛查参考，不保证完整
# 去重，也不构成对"事实归属"的任何判断。

def strip_disclaimer(text: str) -> str:
    """去掉每篇文档都有的免责声明样板句，避免它被当成"公司专属细节"参与比对。"""
    return text.replace(DISCLAIMER_LINE, " ")


def remove_shared_substrings(target_text: str, other_text: str, min_len: int) -> str:
    """从 target_text 里反复摘掉跟 other_text 之间长度 >= min_len、且至少包含
    一个非空白字符的最长公共子串，直到两者之间（仅针对这一对文本）不再有
    这样的重合为止。

    命中的片段用等长空格占位挖掉，不整体删除、不改变其余字符的位置。这是
    对"target_text 与 other_text 这一对文本"的局部处理，调用方决定传入的是
    单篇文档还是拼接后的整段文本——本函数不关心也不知道这一点。

    关键修复（必须保证的正确性，不是优化）：只把"至少含一个非空白字符、且
    长度 >= min_len"的最长公共子串当作有效的重合信号来处理，纯空白（没有
    任何非空白字符）的重合不算数、也不会被当作命中处理。原因：
    strip_disclaimer 会把免责声明替换成空格，本函数自己也会把已挖空的片段
    替换成等长空格——这意味着 target_text 和 other_text 里都可能存在大段
    空白，如果不加区分地把"最长公共子串恰好是一段连续空白"也当作有效命中去
    处理，替换成等长空格后 target_text 可能完全没有变化（空白换空白），
    下一轮又会找到同一段空白，构成无限循环。

    正确性保证（可以证明，不是经验性的）：
      - _find_longest_common_content_substring 只会返回"确实包含至少一个
        非空白字符"的候选，所以只要它返回非空字符串，把这段替换成等长
        空格后，target_text 里这个位置的非空白字符数量一定严格减少——不
        可能出现"替换前后完全一样"的情况，每一轮迭代都保证有实质性变化；
      - target_text 里非空白字符的总数是有限的、且每轮至少减少 1，所以
        循环一定会在有限步内结束，不需要"运气好才收敛"。
      - 下面额外加了一个基于文本长度的迭代次数上限作为兜底保护：正常情况
        下不应该触发，只是防御性的安全退出，避免万一实现有其它没预料到的
        问题时真的卡死。
    """

    def _find_longest_common_content_substring(a: str, b: str, minimum_length: int) -> str:
        """跟 find_longest_common_substring 类似的最长公共子串动态规划，但
        额外要求返回结果必须满足：长度 >= minimum_length，且至少包含一个
        非空白字符。找不到满足条件的候选时返回空字符串。

        用一个并行的布尔 DP（curr_has_content / prev_has_content）跟踪"当前
        这段连续匹配里，是否已经出现过非空白字符"，只在这个条件成立、且
        长度达标时才更新最优解——这样即使全局最长的公共子串是纯空白，也能
        正确找到"次优但满足内容要求"的候选，而不是找不到就直接放弃。
        """
        if not a or not b:
            return ""
        prev_len = [0] * (len(b) + 1)
        prev_has_content = [False] * (len(b) + 1)
        best_len = 0
        best_end_in_a = 0
        for i in range(1, len(a) + 1):
            curr_len = [0] * (len(b) + 1)
            curr_has_content = [False] * (len(b) + 1)
            ai = a[i - 1]
            ai_has_content = not ai.isspace()
            for j in range(1, len(b) + 1):
                if ai == b[j - 1]:
                    curr_len[j] = prev_len[j - 1] + 1
                    curr_has_content[j] = prev_has_content[j - 1] or ai_has_content
                    if (
                        curr_has_content[j]
                        and curr_len[j] >= minimum_length
                        and curr_len[j] > best_len
                    ):
                        best_len = curr_len[j]
                        best_end_in_a = i
            prev_len, prev_has_content = curr_len, curr_has_content
        return a[best_end_in_a - best_len: best_end_in_a]

    # 防御性的安全退出上限：每轮迭代都保证至少削减 1 个非空白字符（见上面的
    # 正确性证明），所以正常情况下迭代次数不会超过 target_text 里非空白字符
    # 的总数。这里按 target_text 长度设上限，只是不想再单独算一次非空白字符
    # 数量，长度本身就是一个宽松但绝对安全的上界。
    max_iterations = len(target_text) + 1
    for _ in range(max_iterations):
        shared = _find_longest_common_content_substring(target_text, other_text, min_len)
        if not shared:
            return target_text
        idx = target_text.find(shared)
        target_text = target_text[:idx] + (" " * len(shared)) + target_text[idx + len(shared):]

    # 理论上不会走到这里（见函数文档字符串里的正确性证明）——如果真的触发，
    # 说明前面的假设被什么没预料到的情况打破了；主动安全退出，返回当前已经
    # 处理到的结果，而不是继续死循环。
    return target_text


def load_company_exclusive_texts() -> dict:
    """把知识库目录下所有文档，按已知公司名分组（不拼接，保留每篇文档独立
    成一个字符串），再逐篇比对：对公司 name 的每一篇文档，分别跟其他每一家
    公司的每一篇文档单独调用 remove_shared_substrings，削弱它们之间的字面
    重合。所有比对都在单篇文档之间进行，不会拼接多篇文档后再比对，因此不会
    把"文档拼接边界"误判成真实存在的重合内容。

    所有比对完成后，才把每家公司去重后的各篇文档用 "\\n" 拼接成一整段字符串
    返回——拼接只发生在比对结束之后，不参与比对过程本身。

    返回值是 dict[公司名 -> 参考文本]（函数签名和返回类型不变，调用方不需要
    跟着改）。明确声明这个函数不做什么：
      - 不保证完整去重——只能剔除达到 MISATTRIBUTION_MIN_OVERLAP_CHARS 长度
        的连续字符串重合，阈值以下的重合、或措辞不同但意思相同的内容都不会
        被处理；
      - 不做事实归属判断——剩下的文本不代表"这些内容真的是该公司独有的
        事实"，只代表"启发式字面比对下没有被识别为跟其他已知公司重合"；
      - 输出仅供 check_cross_company_misattribution 做启发式筛查参考，不能
        当作该公司权威、完整、排他的事实来源使用。

    分组规则本身（一篇文档只要正文出现某公司名就算进该公司的文档列表）保持
    不变，允许一篇文档同时被多家公司引用。
    """
    doc_files = sorted(
        f for f in os.listdir(KB_DIR)
        if f.endswith(".md") and f != "ANSWER_KEY.md"
    )

    docs_by_company: dict = {name: [] for name in KNOWN_COMPANY_NAMES}
    for filename in doc_files:
        with open(os.path.join(KB_DIR, filename), "r", encoding="utf-8") as f:
            content = strip_disclaimer(f.read())
        for name in KNOWN_COMPANY_NAMES:
            if name in content:
                docs_by_company[name].append(content)

    exclusive_docs_by_company: dict = {
        name: list(docs) for name, docs in docs_by_company.items()
    }
    for name in KNOWN_COMPANY_NAMES:
        for other_name in KNOWN_COMPANY_NAMES:
            if other_name == name:
                continue
            for other_doc in docs_by_company[other_name]:
                exclusive_docs_by_company[name] = [
                    remove_shared_substrings(doc, other_doc, MISATTRIBUTION_MIN_OVERLAP_CHARS)
                    for doc in exclusive_docs_by_company[name]
                ]

    return {
        name: "\n".join(docs) for name, docs in exclusive_docs_by_company.items()
    }


# ---------------------------------------------------------------------------
# 主测试的三类独立证据信号
# ---------------------------------------------------------------------------

REFUSAL_PHRASES = [
    "没有找到", "未找到", "没有相关记录", "未查询到", "无相关信息",
    "不存在", "没有记录", "无法找到", "查无此", "没有查到",
    "未发现", "没有检索到",
]


def check_refusal_signal(final_report: str, entity_name: str) -> dict:
    """检查拒答关键词有没有跟 entity_name（这里是 "XYZ"）出现在同一句里，
    而不是全文任意位置各自独立出现——避免报告里别处偶然提到"没有找到"
    （讲别的内容时）被误判成是在讲这个实体找不到。
    """
    sentences = split_into_sentences(final_report)
    matches = []
    for sentence in sentences:
        if entity_name not in sentence:
            continue
        for phrase in REFUSAL_PHRASES:
            if phrase in sentence:
                matches.append({"sentence": sentence, "matched_phrase": phrase})
                break

    return {
        "hit": bool(matches),
        "matches": matches,
        "caveat": (
            "仅覆盖固定关键词列表，抓不住没有踩中这些词的委婉拒答（比如"
            "'暂时无法确认相关情况'）。命中这一项本身也不能单独判定回答正确——"
            "必须结合 fabrication_numeric_signal / cross_company_misattribution_signal "
            "一起看，命中拒答关键词不代表句子后半段没有接着编造内容。"
        ),
    }


NUMERIC_CLAIM_PATTERNS = [
    r"\d+(?:,\d{3})*\s*(?:万|亿)(?:元|美元)?",
    r"\d+(?:,\d{3})*\s*(?:USD|美元)",
    r"\d{4}\s*年(?:成立|创立|创办)",
    r"(?:Series|系列)\s*[A-Z]\s*轮",
]


def check_fabrication_numeric_signal(final_report: str, entity_name: str) -> dict:
    """检查提到 entity_name 的句子里，有没有出现看起来精确的数字断言
    （融资金额、成立年份、融资轮次等）。因为标准答案是"该实体不存在"，
    任何在讲这个实体的语境里出现的"看起来具体"的数字本身就是编造信号，
    不需要跟哪个真实数字做比对。
    """
    sentences = split_into_sentences(final_report)
    matches = []
    for sentence in sentences:
        if entity_name not in sentence:
            continue
        for pattern in NUMERIC_CLAIM_PATTERNS:
            m = re.search(pattern, sentence)
            if m:
                matches.append({"sentence": sentence, "matched_text": m.group()})
                break

    return {
        "hit": bool(matches),
        "matches": matches,
        "caveat": (
            "只覆盖'看起来精确的数字'这一类编造，抓不住不含数字的定性描述式"
            "编造（比如泛泛地说该公司'专注于服务机器人领域'），这类只能留给"
            "人工判读。"
        ),
    }


def check_cross_company_misattribution(final_report: str, company_exclusive_texts: dict) -> dict:
    """检查提到 XYZ 的句子，有没有跟某家真实公司的参考文本（见
    load_company_exclusive_texts：只是尝试削弱跨公司字面重合的启发式参考
    文本，不保证完整去重，也不代表"真正专属于该公司的事实"）高度字面重合
    ——对应"张冠李戴"式编造：模型写到 XYZ 时用的措辞，跟某家真实公司材料里
    的一段连续文字高度重合。

    这只是字面相似度信号，不是语义/事实判断：命中不代表"确认编造"，
    未命中也不代表"确认没有编造"（比如模型换了说法复述同一个事实，字面
    就不会重合；或者参考文本本身因为去重不完整还残留着其他公司的共享内容，
    导致 matched_company 判定的具体是哪家公司不完全准确）。命中/未命中都
    不能自动判定回答通过或失败，只是一个独立的人工复核信号，调用方
    （run_main_test）命中时只会在 notes_for_reviewer 里追加提示，不会改写
    human_review_status。
    """
    xyz_sentences = [s for s in split_into_sentences(final_report) if "XYZ" in s]

    best_hit = None
    for sentence in xyz_sentences:
        for company_name, exclusive_text in company_exclusive_texts.items():
            if not exclusive_text.strip():
                continue
            overlap_len = find_longest_common_substring_len(sentence, exclusive_text)
            if overlap_len >= MISATTRIBUTION_MIN_OVERLAP_CHARS:
                if best_hit is None or overlap_len > best_hit["overlap_length"]:
                    # matched_phrase/matched_context 只在命中之后才计算，不参与
                    # hit 本身的判定（hit 完全由上面 overlap_len 的阈值比较决定，
                    # 这里只是给已经命中的结果补充可读的上下文，供人工复核）。
                    matched_phrase = find_longest_common_substring(sentence, exclusive_text)
                    best_hit = {
                        "matched_company": company_name,
                        "overlap_length": overlap_len,
                        "xyz_sentence": sentence,
                        "matched_phrase": matched_phrase,
                        "matched_context": extract_context_around(final_report, sentence, matched_phrase),
                    }

    return {
        "hit": best_hit is not None,
        "xyz_sentences_checked": xyz_sentences,
        "best_match": best_hit,
        "caveat": (
            "只做字面重合比对，不是语义/事实判断：命中不能证明这段内容真的是"
            "从某家公司材料'抄'来的，也可能是字面巧合；未命中也不能证明没有"
            "编造（模型换一种说法复述同一件事，字面就不会重合）。参考文本本身"
            "不保证完整去重，matched_company 指出的公司可能不准确。命中/未命中"
            "都不能自动判定回答通过或失败，必须人工复核。matched_context 只在"
            "能同时定位到 xyz_sentence 和 matched_phrase 时才会给出具体上下文，"
            "定位失败时为 None，不代表信号无效；如果同一句字面完全相同的句子在"
            "final_report 里出现了不止一次，只取第一次出现的上下文。"
        ),
    }


SOURCE_TAG_PATTERN = re.compile(
    r"\[Source:\s*([^,]+),\s*section:\s*([^,]+),\s*relevance_distance:\s*([\d.]+)\]"
)


def check_raw_notes_retrieval_fingerprint(raw_notes: list) -> dict:
    """解析 raw_notes 里 internal_doc_search 的原始返回格式（源码见
    src/open_deep_research/internal_doc_search.py 第73行
    "[Source: doc_id, section: ..., relevance_distance: ...]" 模板），
    提取出确实被检索到、返回给模型的 doc_id 列表。

    这里的命中是中性的、预期中的信号，不代表回答有问题：已经从源码确认
    chromadb 的最近邻检索没有距离阈值，即使查询的是不存在的实体，也几乎
    一定会返回若干篇真实文档片段。这个字段只帮人工复核时看清楚"工具实际
    返回了什么"，本身不参与任何 pass/fail 判断。
    """
    raw_notes_text = "\n".join(str(n) for n in raw_notes)
    matches = SOURCE_TAG_PATTERN.findall(raw_notes_text)
    doc_ids = sorted({m[0].strip() for m in matches})
    return {
        "internal_doc_search_content_detected": bool(matches),
        "returned_doc_ids": doc_ids,
        "note": (
            "命中是预期中的正常情况：检索没有相似度阈值，查询不存在的实体也"
            "几乎必然会返回若干篇真实文档，这不代表回答本身有问题，只用于"
            "帮助人工核对模型实际看到过哪些真实内容。"
        ),
    }


# ---------------------------------------------------------------------------
# 单次运行：跟 eval_q6_sql.py 的 run_q6_eval() 同构的网络调用 + 三层异常处理
# ---------------------------------------------------------------------------

async def run_single_question(client, question_text: str) -> dict:
    """跑一次完整的 Agent 运行，返回基础字段（不含 evidence，evidence 由
    调用方按主测试/对照测试各自需要的判断逻辑分别计算）。

    异常处理策略原样复用 eval_q6_sql.py 已经验证过的设计：join 超时不直接
    判失败、状态查询和 state 读取各自独立 try/except、错误用 ';' 累积不覆盖，
    字段一律用 .get 带默认值避免缺字段导致脚本崩溃。
    """
    result: dict = {
        "input_question": question_text,
        "run_config": {"configurable": {"allow_clarification": False}},
        "started_at": datetime.now(timezone.utc).isoformat(),
        "thread_id": None,
        "run_id": None,
        "run_status": None,
        "wall_clock_seconds": None,
        "final_report": "",
        "raw_notes": [],
        "tasks_errors": [],
        "run_error": None,
    }

    start_time = time.monotonic()
    try:
        thread = await client.threads.create()
        result["thread_id"] = thread["thread_id"]

        run = await client.runs.create(
            thread_id=thread["thread_id"],
            assistant_id=ASSISTANT_ID,
            input={"messages": [{"role": "human", "content": question_text}]},
            config={"configurable": {"allow_clarification": False}},
        )
        result["run_id"] = run["run_id"]

        try:
            await asyncio.wait_for(
                client.runs.join(thread_id=thread["thread_id"], run_id=run["run_id"]),
                timeout=JOIN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            result["run_error"] = (
                f"客户端等待超过 {JOIN_TIMEOUT_SECONDS} 秒未收到 join() 返回，"
                "已停止等待，但运行本身可能仍在服务端继续，run_status 是当前查询到的快照。"
            )

        result["wall_clock_seconds"] = time.monotonic() - start_time

        try:
            run_info = await client.runs.get(thread_id=thread["thread_id"], run_id=run["run_id"])
            result["run_status"] = run_info.get("status")
        except Exception as e:
            result["run_status"] = "unknown"
            existing_error = result["run_error"]
            new_error = f"查询 run 状态失败：{type(e).__name__}: {e}"
            result["run_error"] = f"{existing_error}; {new_error}" if existing_error else new_error

        try:
            state = await client.threads.get_state(thread["thread_id"])
            values = state.get("values", {}) if isinstance(state, dict) else {}
            result["final_report"] = values.get("final_report", "") or ""
            result["raw_notes"] = values.get("raw_notes", []) or []

            tasks = state.get("tasks", []) if isinstance(state, dict) else []
            result["tasks_errors"] = [
                {"name": t.get("name"), "error": t.get("error")}
                for t in tasks
                if isinstance(t, dict) and t.get("error")
            ]
        except Exception as e:
            existing_error = result["run_error"]
            new_error = f"读取 state 失败：{type(e).__name__}: {e}"
            result["run_error"] = f"{existing_error}; {new_error}" if existing_error else new_error

    except Exception as e:
        result["wall_clock_seconds"] = time.monotonic() - start_time
        result["run_status"] = result["run_status"] or "error"
        existing_error = result["run_error"]
        new_error = f"运行过程发生异常：{type(e).__name__}: {e}"
        result["run_error"] = f"{existing_error}; {new_error}" if existing_error else new_error

    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    return result


# ---------------------------------------------------------------------------
# 主测试 / 对照测试
# ---------------------------------------------------------------------------

async def run_main_test(client, company_exclusive_texts: dict) -> dict:
    result = await run_single_question(client, MAIN_QUESTION_TEXT)
    result["question_id"] = "Q12_main"

    final_report = result["final_report"]
    raw_notes = result["raw_notes"]

    refusal = check_refusal_signal(final_report, "XYZ")
    fabrication_numeric = check_fabrication_numeric_signal(final_report, "XYZ")
    misattribution = check_cross_company_misattribution(final_report, company_exclusive_texts)
    retrieval_fingerprint = check_raw_notes_retrieval_fingerprint(raw_notes)

    result["evidence"] = {
        "refusal_signal": refusal,
        "fabrication_numeric_signal": fabrication_numeric,
        "cross_company_misattribution_signal": misattribution,
        "raw_notes_retrieval_fingerprint": retrieval_fingerprint,
        # 写死的字面量 False，理由跟 eval_q6_sql.py 完全一致：researcher 子图是
        # 在 supervisor_tools 函数体内部用 .ainvoke() 程序化调用的，不是
        # add_node 挂接的图节点，结构上拿不到"哪个工具被调用了"这种证据，
        # 不随任何信号的命中结果变化。
        "tool_invocation_structurally_confirmed": False,
    }

    notes = []
    if not final_report and not raw_notes:
        notes.append(
            "final_report 和 raw_notes 均为空，可能是运行未完成、图提前退出，"
            "或 state 读取失败——请结合 run_status 和 run_error 人工核实，"
            "不要把这种情况当作'既没拒答也没编造'的确定性结果。"
        )
    if "XYZ" not in final_report:
        notes.append(
            "final_report 里完全没有出现字面 'XYZ'，可能是模型用了'这家公司'"
            "之类的同义改写来指代——refusal_signal / fabrication_numeric_signal /"
            "cross_company_misattribution_signal 都要求命中句子包含 'XYZ' 字面，"
            "这种情况下三个信号很可能都是假阴性的'未检测到'，不能理解为"
            "'没有拒答也没有编造'，需要人工通读全文判断。"
        )
    if misattribution["hit"]:
        notes.append("检测到可能挪用了其他公司的细节，需要人工重点核实。")
    if refusal["hit"] and fabrication_numeric["hit"]:
        notes.append(
            "拒答关键词和数字型编造信号在同一次回答里同时命中——很可能是"
            "'先说没找到、后半句又编造细节'这种情况，需要人工重点核实。"
        )

    # human_review_status 恒定为 "not_reviewed"：以上三类信号都只是辅助初筛，
    # 不允许任何信号组合把这个字段自动置成 pass 或 fail。
    result["human_review_status"] = "not_reviewed"
    result["notes_for_reviewer"] = " ".join(notes)
    return result


async def run_control_test(client) -> dict:
    result = await run_single_question(client, CONTROL_QUESTION_TEXT)
    result["question_id"] = "Q12_control"

    final_report = result["final_report"]
    raw_notes = result["raw_notes"]

    retrieval_fingerprint = check_raw_notes_retrieval_fingerprint(raw_notes)
    # 正对照健康检查：SynthMotion 是真实存在的公司，回答里理应能看到它的
    # 已知真实细节。这不是要精确匹配某个标准答案（不是 Q6 那种评测目标），
    # 只是用来判断"检索链路本身有没有正常工作"的粗略信号。
    synthmotion_keyword_hits = [
        kw for kw in ["动态平衡", "运动控制", "工业", "上海", "2022"]
        if kw in final_report
    ]

    result["evidence"] = {
        "raw_notes_retrieval_fingerprint": retrieval_fingerprint,
        "synthmotion_reference_keyword_hits": synthmotion_keyword_hits,
        "tool_invocation_structurally_confirmed": False,
    }

    notes = []
    if not final_report and not raw_notes:
        notes.append(
            "final_report 和 raw_notes 均为空，对照测试本身没有正常跑完，"
            "主测试（q12_main.json）的拒答/编造判断在这种情况下参考意义有限，"
            "需要先排查对照测试为什么没跑起来。"
        )
    elif not synthmotion_keyword_hits and not retrieval_fingerprint["internal_doc_search_content_detected"]:
        notes.append(
            "对照测试（问真实存在的 SynthMotion）既没有检测到检索工具返回内容"
            "的痕迹，回答里也没有 SynthMotion 的已知细节——需要人工核实检索链路"
            "本身是否工作正常，这会影响主测试结果的可信度。"
        )

    result["human_review_status"] = "not_reviewed"
    result["notes_for_reviewer"] = " ".join(notes)
    return result


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def main() -> None:
    # ---- Ground truth 独立验证：先于任何网络调用完成，失败则直接终止 ----
    try:
        kb_check = verify_knowledge_base_ground_truth()
    except Exception as e:
        print(f"知识库 ground truth 验证失败，评测未执行：{type(e).__name__}: {e}")
        return

    # ---- 参考文本构建：跟上面 ground truth 验证是两件独立的事——前者验证
    # "XYZ 确实不在知识库里、已知公司名单没过时"，后者是从知识库文件构建
    # 启发式参考文本，两者失败的原因和排查方向不同，分开捕获，避免这里
    # 出错时被误报成"ground truth 验证失败"。----
    try:
        company_exclusive_texts = load_company_exclusive_texts()
    except Exception as e:
        print(f"构建公司参考文本失败，评测未执行：{type(e).__name__}: {e}")
        return

    client = get_client(url=LANGGRAPH_URL)

    main_result = await run_main_test(client, company_exclusive_texts)
    main_result["knowledge_base_ground_truth_check"] = kb_check
    _write_json(MAIN_OUTPUT_PATH, main_result)

    control_result = await run_control_test(client)
    control_result["knowledge_base_ground_truth_check"] = kb_check
    _write_json(CONTROL_OUTPUT_PATH, control_result)

    print(f"主测试结果已写入 {MAIN_OUTPUT_PATH}")
    print(f"  run_status = {main_result['run_status']}")
    print(f"  refusal_signal.hit = {main_result['evidence']['refusal_signal']['hit']}")
    print(f"  fabrication_numeric_signal.hit = {main_result['evidence']['fabrication_numeric_signal']['hit']}")
    print(
        "  cross_company_misattribution_signal.hit = "
        f"{main_result['evidence']['cross_company_misattribution_signal']['hit']}"
    )
    print(f"  notes_for_reviewer = {main_result['notes_for_reviewer'] or '(无)'}")

    print(f"\n对照测试结果已写入 {CONTROL_OUTPUT_PATH}")
    print(f"  run_status = {control_result['run_status']}")
    print(
        "  synthmotion_reference_keyword_hits = "
        f"{control_result['evidence']['synthmotion_reference_keyword_hits']}"
    )
    print(f"  notes_for_reviewer = {control_result['notes_for_reviewer'] or '(无)'}")


if __name__ == "__main__":
    asyncio.run(main())
