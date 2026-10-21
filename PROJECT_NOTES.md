# 项目扩展说明：DataAnalyst + RAG + Memory

本文档说明在官方 open_deep_research 基础上做的扩展工作，包括架构决策、
验证过程、已知局限。官方项目本身的说明见 README.md。

## 一、扩展了什么

在官方 Supervisor-Researcher 多智能体架构基础上，新增了三类能力：

1. sql_executor：查询公司内部结构化数据库（只读 SQLite），不做 RAG、
   不建语义层——模型自己探索 schema（查 sqlite_master），报错原样返回
   让模型自己纠正。
2. python_executor：让模型自己写 pandas 代码分析已查询到的数据
   （比如计算标准差这类 SQL 不方便直接做的运算）。
3. internal_doc_search：基于 Chroma 的企业内部文档语义检索（RAG），
   针对非结构化文档（公司介绍、会议纪要、行业报告）。

以及一套端到端评测框架（Q6/Q12 两道代表性题目）和跨会话记忆（mem0）。

## 二、架构设计要点

### 角色划分逻辑

不按"内部数据 vs 外部数据"划分角色，按"找资料 vs 算数字"划分：
- 找资料（语义检索，没有精确数字答案）：tavily_search（网页）+
  internal_doc_search（内部文档） 归 GeneralResearcher
- 算数字（结构化查询加计算，有精确答案）：sql_executor 加
  python_executor 归 DataAnalyst

理由：真实企业场景里，一次研究任务经常连续问外部行业情况、内部数据库
数字、内部文档记录，这三类问题不该因为数据在内部还是外部这个维度被拆
成不相关的角色；但找资料和算数字在技术路径上（语义检索对比查询计算）
是真正不同的，值得分开。

### DataAnalyst 目前是 Prompt 层面的角色约定，不是代码层面的独立节点

三个新工具通过 get_all_tools 全局注册，所有 Researcher 都能访问，
没有代码层面的工具集隔离。角色区分完全依赖 lead_researcher_prompt 里
的路由指令。

这是一个明确的架构决策，不是遗漏：ConductResearch 这个委托工具只有
一个字符串字段 research_topic，没有结构化的角色字段。真正的代码层面
隔离在技术上可行，且已有具体设计方案，但判断为当前项目阶段优先级较低，
作为路线图条目记录在 my_extensions/IMPROVEMENTS.md 里，未实施。

## 三、评测：Q6 与 Q12

自己设计的一套端到端评测框架，核心原则是三层证据分离，不允许用弱证据
冒充强证据：

1. 最终答案层：报告内容是否符合标准答案（最弱证据，模型可能蒙对）
2. 间接指纹层：中间过程记录里有没有出现像是工具真实输出格式的内容
3. 结构化调用证据层：有没有独立记录证明某个工具被调用（这一层在当前
   架构下确认拿不到，Researcher 子图是程序化调用而非图节点，这是已
   验证的架构限制，不是测试没写好）

### Q6：内部数据库查询

测试内部融资信息查询（唯一正确数据源是数据库，知识库文档故意不含
具体数字，用来测路由是否正确）。首次真实运行发现一个评测规则本身的
假阴性 bug（模型把日期格式转写成中文表达，字符串精确匹配判定为不匹配），
定位、修复、补充单元测试后确认根因是评测规则问题，不是被测系统的问题。

### Q12：拒答与张冠李戴检测

测试知识库里有没有记录一个不存在的公司，验证系统会不会诚实说明没有
记录。检测器本身经过多轮迭代修复（变量命名一致性、跨文档拼接产生的
虚假重合、纯空格字符触发的死循环风险）。

人工复核发现两个细节问题：主测试报告声称检索了实际不存在的检索渠道
（表述夸大）；对照测试报告夹杂了没有原文依据的扩写内容。

ANSWER_KEY.md 里共设计了 12 道测试题，目前完整走完闭环的是 Q6 和 Q12
两道代表性题目，另有 RAG 检索能力的独立对照实验（详见 IMPROVEMENTS.md
里的三轮实验记录）。不构成完整的 benchmark，只作为方法论演示。

## 四、Memory（mem0）

在 write_research_brief 节点接入跨会话记忆：生成研究简报之前先检索
该用户的历史记忆并拼入 prompt；调用模型生成简报之后，保存这次用户
输入的原始文本。

- DEMO_USER_ID 是固定的单用户演示身份，不是真实的多用户认证系统
  （用不同 user_id 单独验证过隔离机制本身有效，但图接入时用的是固定ID）
- save_memory 存的是用户原始输入文本，不是模型生成的摘要
- 跨线程写入检索链路有直接的运行日志证据（新线程的检索调用返回了旧
  线程写入的记忆内容）；检索结果是否真正被模型采纳、体现在最终生成
  内容里，只有一次间接的观察性证据，不是结构化追踪得到的直接证据
- 持久化存储：在后续测试运行中观察到早前写入的记忆仍可被检索；这属于
  观察性证据，尚未通过专门设计的进程重启测试验证持久化行为

## 五、如何运行

安装依赖（本地开发和测试使用的项目虚拟环境）：
uv sync

初始化演示数据（注意：这个脚本会先删除已有数据库文件，再重新创建，
每次运行都会重置成同一份初始数据，不是增量追加）：
uv run python my_extensions/setup_demo_db.py
uv run python my_extensions/build_knowledge_base.py

启动服务（uvx 会拉起一个独立于上面 uv sync 项目虚拟环境的隔离运行环境，
--python 3.11 是这个隔离环境使用的解释器版本；本地开发和测试用 uv run
在项目虚拟环境（3.13）下进行，两者是两套独立环境，具体依赖兼容性以
实际运行结果为准）：
uvx --refresh --from "langgraph-cli[inmem]" --with-editable . --python 3.11 langgraph dev --allow-blocking

工具单元测试：
uv run python my_extensions/test_sql_executor.py
uv run python my_extensions/test_python_executor.py
uv run python my_extensions/test_internal_doc_search.py
uv run python my_extensions/test_memory_manager.py

## 六、已知局限（完整列表见 my_extensions/IMPROVEMENTS.md）

- python_executor 的沙箱不是生产级隔离（已验证存在架构性绕过路径）
- internal_doc_search 的路由依赖用户提问措辞包含内部这类提示词，
  否则可能不触发内部检索（已用真实案例验证过这个问题存在）
- 评测覆盖面小，不是完整 benchmark
- Memory 用固定单用户 ID，不是真实多用户系统；持久化只有观察性证据，
  没有专门设计的重启测试
