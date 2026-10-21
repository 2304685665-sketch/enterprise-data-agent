"""工具调用可靠性原语：可重试/不可重试异常类型，供 execute_tool_safely 做重试判断。

设计前提（写在这里，不要让人去猜）：
- 分类依据的是真实抛出的异常类型，不解析任何返回字符串的前缀或内容——字符串
  前缀容易因为措辞调整而失效，异常类型是显式、可追溯的契约。
- 当前全部注册的自建工具都是只读或无副作用操作：sql_executor 只允许 SELECT
  （数据库连接层面强制只读，见 sql_executor.py 的 mode=ro + authorizer）；
  python_executor 在一次性容器里执行，容器销毁后不留下任何状态；
  internal_doc_search 是只读的向量检索。因此对"瞬时性错误"做有限次数重试是
  安全的——不存在"重试导致某个副作用被多执行一次"的风险。如果将来引入有
  副作用的工具（比如真正的写操作工具），必须重新评估这个前提，不能直接把
  这里的 retryable 分类逻辑套用过去。
- 这些异常类型只在"要不要重试"这条内部判断路径上使用。各工具对外暴露的
  公开函数（被直接 `.ainvoke()` 调用时，比如现有的测试）行为不受影响——
  这些测试走的从来都不是会抛出这里定义的异常的代码路径。
"""


class ToolError(Exception):
    """工具执行失败的标记基类，仅供 execute_tool_safely 内部识别使用。"""


class TransientToolError(ToolError):
    """瞬时性失败：这次失败大概率和"这次调用恰好赶上的外部状态"有关
    （Docker daemon 暂时连不上、容器一时起不来、Embedding API 网络抖动），
    再试一次有意义。各工具内部在明确识别出这类失败时显式抛出，不是由
    execute_tool_safely 去猜。
    """


class NonRetryableInfrastructureError(ToolError):
    """基础设施/环境层面的失败，但刻意选择不重试——和 TransientToolError
    （值得重试）、以及"工具自己把失败处理成字符串正常返回"的 tool_input_error
    都不是一回事。

    目前唯一的真实使用场景：internal_doc_search 的超时（`asyncio.wait_for`
    放弃等待之后）。放弃等待不等于真正取消——后台线程里真正的 Embedding API
    请求并没有被打断，还在继续跑，只是结果被丢弃。如果这时候立即重试，会
    在已经有一个"被放弃但仍在运行"的请求线程的情况下又发起一个新的，连续
    几次超时快速重试会让这类线程持续堆积。这是刻意不重试的具体原因，不是
    随手定的规则。对 execute_tool_safely 而言，这类异常要计入熔断
    （infrastructure_error）但不进入重试循环。
    """


class PermanentToolError(ToolError):
    """确定性失败：代码/语法/参数本身有问题，或者本地资源确实超限——
    重试不会改变结果，只会浪费时间获得同样的失败。

    当前版本里没有任何工具显式抛出这个类型：各工具已有的"确定性失败"
    分支（SQL 语法错误、Python 代码报错、执行超时等）本来就是直接返回
    描述性字符串，从不抛异常，所以对 execute_tool_safely 而言它们天然
    就是"不重试"的——不需要额外包一层 PermanentToolError 才能达到同样
    的效果。保留这个类型是为了：(a) 和 TransientToolError 构成完整、
    对称的分类词汇表，便于未来新工具直接复用；(b) 如果某个工具未来想要
    更明确地表达"这是我确认过的确定性失败"而不是依赖"没抛异常=不重试"
    这种隐式约定，可以显式抛出它。
    """


# execute_tool_safely 统一按"异常类型"而不是字符串内容识别网络层瞬时错误，
# 覆盖面来自 openai SDK（internal_doc_search 调用 Embedding API 时可能触发）。
# 之所以在这里而不是在 internal_doc_search.py 里做 try/except 转换成
# TransientToolError，是为了让"识别网络异常"这件事对任何工具都通用，不需要
# 每个新工具都重新写一遍同样的 except 列表——但 internal_doc_search 仍然需要
# 让这些异常"逃出"自己的函数体，不能被自己的 except Exception 提前吞掉
# （见 internal_doc_search.py 里对这几个类型的显式 re-raise）。
try:
    import openai

    TRANSIENT_NETWORK_EXCEPTION_TYPES: tuple[type[BaseException], ...] = (
        openai.APIConnectionError,
        openai.APITimeoutError,
    )
except ImportError:  # pragma: no cover - openai 在本项目里始终是依赖，这里只是防御
    TRANSIENT_NETWORK_EXCEPTION_TYPES = ()
