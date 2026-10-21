"""python_executor 工具 —— DataAnalyst 专精工具之二。

设计理念：让模型自己写 pandas 代码去分析数据（比如算平均值、算增长率），
而不是提前写死计算逻辑。

安全边界（容器化版本，2026-10-09，加固版本，2026-10-09 第二轮）：
- 真正的隔离边界现在是 Docker 容器本身，不再是 Python 应用层的
  __builtins__/__import__ 白名单。旧方案已在 IMPROVEMENTS.md 里被
  记录为可绕过（pd.__builtins__ 泄漏真实 __import__；
  ().__class__.__bases__[0].__subclasses__() 枚举到 subprocess.Popen），
  继续在 Python 层加规则属于治标不治本，因此本版本直接移除了该白名单。

  重要措辞澄清：下面这些参数降低了特定风险的发生概率/影响范围，
  不构成"绝对安全边界"的证明——没有做过内核级漏洞的对抗性测试，
  Docker 本身的隔离也不是无懈可击的（历史上存在过容器逃逸 CVE）。

- 每次调用都在一个一次性容器里执行，强制参数：
    --network none                容器内无法访问任何网络（已实测：真实 OSError，不是应用层拦截）
    --read-only + --tmpfs /tmp     根文件系统只读，仅 /tmp 可写，且显式限定大小（见下方 TMPFS_SIZE）
    --memory / --memory-swap       必须设为同一值，缺一不可——
                                    实测确认：只设 --memory 不设 --memory-swap，
                                    Docker 默认允许 swap 到约 2 倍内存，
                                    256m 限制下分配 1GB 照样会"成功"，两者都设才会被 OOM-kill
    --cpus                         限制 CPU 占用
    --pids-limit                   限制容器内进程/线程总数，防 fork bomb
                                    （已实测：limit=64 时，fork 循环在第 63 个子进程后
                                    收到 EOTHER/EAGAIN 风格的 OSError，被正确拦住）
    --cap-drop=ALL                 丢弃所有 Linux capabilities（CAP_NET_RAW/CAP_SYS_ADMIN等）。
                                    兼容性风险：如果将来镜像里的代码需要任何特殊能力
                                    （比如 ping 需要 CAP_NET_RAW），会直接失败；
                                    当前只跑纯 Python/pandas，没有这类需求，已实测基本执行不受影响
    --security-opt=no-new-privileges  禁止通过 setuid/setgid 二进制提权。
                                    兼容性风险：几乎为零，本镜像内没有任何 setuid 程序
    --pull=never                   镜像必须已在本机构建好，禁止隐式联网拉取
    不挂载任何宿主机目录             容器内看不到宿主机文件系统的任何内容（已实测验证）
    保留 Docker 默认 seccomp profile  没有传 --security-opt seccomp=unconfined，
                                    也没有自定义 profile——按要求维持默认

- 代码通过 stdin 喂给容器内的 python3（镜像 ENTRYPOINT 为 `python3 -u -`），
  不挂载卷、不写入命令行参数，避免注入和参数长度问题。提交的代码本身有
  MAX_CODE_BYTES 字节上限，超出直接拒绝（不启动容器）。

- stdout/stderr 采集：不再用 `subprocess.run(capture_output=True)`——那个
  调用会把子进程的全部输出无上限地攒在内存里，一个打印几十MB的死循环就能
  把宿主机内存吃满。现在用 Popen + 两个后台线程分别持续排空 stdout/stderr
  管道，每次只 read() 一个 chunk，按字节数计数，超过限制后不再往缓冲区里
  追加，但仍然继续读取并丢弃，直到 EOF —— 这是为了不让子进程因为管道写满
  而被阻塞死锁，不是"读够了就不读了"。解码在全部读取结束后一次性做，用
  errors="replace"，避免因为正好在截断点切开一个多字节 UTF-8 字符而抛异常。
  已用 50MB 量级的压力测试验证：返回内容确实被限制住，不会把 50MB 都搬进
  Python 这边的内存。

  stdout 和 stderr 用了不同的截断策略（有意为之，不是疏漏）：
    * stdout：只保留开头 MAX_OUTPUT_BYTES 字节（_BoundedStreamReader）。
      已知取舍：如果真正想看的结果恰好在超长输出的末尾，会被截掉——
      对 stdout 来说这个代价可以接受，模型应该用 print() 输出关键结果
      而不是先打印大量无关内容
    * stderr：保留开头 STDERR_HEAD_BYTES（2KB）+ 结尾 STDERR_TAIL_BYTES
      （6KB），中间丢弃（_HeadTailStreamReader）。这是专门为了修复一个
      实测验证过的真实问题：错误/异常信息几乎总是出现在 stderr 的末尾
      （Python traceback 的最后一行才是真正的异常类型和消息），只保留
      开头会恰好丢掉这条最有价值的信息；head+tail 两段式能同时保留
      "最先出现的上下文" 和 "最后的真正报错"

- 超时与清理（本轮重写）：
    * `docker kill` 本身带超时（CLEANUP_KILL_TIMEOUT），不会无限等待一个
      卡住的 Docker CLI
    * kill 之后会用 `docker inspect` 轮询确认容器确实消失了
      （有限轮询，CLEANUP_CONFIRM_TIMEOUT 内），而不是假设 kill 命令
      返回成功就代表容器已经停止——这两者不是一回事，已用实测确认
      （正常情况下 --rm 清理非常快，~100ms 级别），但轮询仍然是必要的
      保险，不能把"Docker CLI 超时"直接当成"容器还在运行"
    * 如果轮询超时仍未确认清理，会在返回的错误信息里如实说明
      "无法确认容器已被清理"，不会声称清理成功

- 已知局限（加固之后仍然存在，诚实记录，不是已解决）：
    * 依赖本机已安装并运行 Docker，且镜像
      my_extensions/docker/python_executor/Dockerfile 已提前 build 好
      （tag: odr-python-executor:latest）——这是新增的硬性运行环境依赖，
      没有宿主机执行回退（故意不做，避免"安全降级"）
    * 每次调用都要起一个新容器，有额外的启动延迟（实测约 0.3-1 秒）
    * 没有做真正的多租户资源隔离（多个并发调用之间只各自有自己的
      --cpus/--memory 上限，没有额外的跨调用调度隔离）
    * 没有做对抗性的内核漏洞测试，Docker 的隔离边界不是绝对的
    * --cap-drop=ALL 和 --pids-limit 是本轮新加的，只验证了"不影响正常
      pandas 执行"和"pids-limit 确实生效"，没有做更大规模的兼容性回归
"""

import asyncio
import subprocess
import threading
import time
import uuid

from langchain_core.tools import tool

from open_deep_research.tool_reliability import TransientToolError, NonRetryableInfrastructureError

IMAGE_NAME = "odr-python-executor:latest"
TIMEOUT_SECONDS = 15
MEMORY_LIMIT = "256m"
CPU_LIMIT = "0.5"
PIDS_LIMIT = "64"
TMPFS_SIZE = "64m"
MAX_OUTPUT_BYTES = 20000  # stdout 的上限，字节为基础，只保留开头（见模块docstring已知取舍）
STDERR_HEAD_BYTES = 2048  # stderr 保留的开头字节数（2KB）
STDERR_TAIL_BYTES = 6144  # stderr 保留的结尾字节数（6KB）——结尾往往是真正的异常信息
MAX_CODE_BYTES = 65536  # 提交代码本身的大小上限（64KB），超出直接拒绝
READ_CHUNK_SIZE = 4096
CLEANUP_KILL_TIMEOUT = 5  # `docker kill` 这条命令本身的超时
CLEANUP_CONFIRM_TIMEOUT = 3  # 轮询确认容器已被移除的总时长上限
CLEANUP_POLL_INTERVAL = 0.2
READER_JOIN_TIMEOUT = 5  # 进程结束后，等待排空线程收尾的上限

PYTHON_EXECUTOR_DESCRIPTION = (
    "Execute Python (pandas) code for data analysis inside an isolated, "
    "disposable Docker container (no network access, no access to the host "
    "filesystem, limited memory/CPU/process-count, and a hard execution "
    "timeout). This reduces -- but, as with any container-based sandbox, "
    "does not absolutely guarantee -- isolation from the host. "
    "The variable pd (pandas) is available; other standard-library modules "
    "are technically importable but cannot reach the network or the host "
    "machine, since isolation is enforced at the container level, not by "
    "restricting which modules you may import. "
    "If you need data to analyze, first query it via sql_executor and "
    "construct a DataFrame from the result yourself. "
    "Use print() to output what you want to see -- only printed output "
    "is returned to you, and it is truncated beyond a size limit."
)


class _BoundedStreamReader:
    """Continuously drains a subprocess pipe on a background thread,
    keeping at most `limit_bytes` of what it reads.

    Draining never stops once the limit is hit -- it keeps reading and
    discarding the excess until EOF. This is deliberate: if we stopped
    reading a pipe once "full", the child process could block forever
    trying to write to it (classic subprocess deadlock), which would
    leave the container running past any timeout we think we've enforced.
    """

    def __init__(self, stream, limit_bytes: int, chunk_size: int = READ_CHUNK_SIZE):
        self._stream = stream
        self._limit = limit_bytes
        self._chunk_size = chunk_size
        self._buf = bytearray()
        self.truncated = False
        self.total_bytes = 0
        self._thread = threading.Thread(target=self._drain, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _drain(self) -> None:
        try:
            while True:
                chunk = self._stream.read(self._chunk_size)
                if not chunk:
                    break
                self.total_bytes += len(chunk)
                if len(self._buf) < self._limit:
                    room = self._limit - len(self._buf)
                    if len(chunk) <= room:
                        self._buf.extend(chunk)
                    else:
                        self._buf.extend(chunk[:room])
                        self.truncated = True
                else:
                    self.truncated = True
        except (ValueError, OSError):
            # 底层流被关掉了（比如进程被 kill）——安静退出，不是需要上报的错误
            pass

    def join(self, timeout: float | None = None) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def text(self) -> str:
        # errors="replace"：截断点如果正好切在一个多字节 UTF-8 字符中间，
        # 不能让 decode 直接抛异常——用替换字符顶上，而不是假装没截断过。
        return self._buf.decode("utf-8", errors="replace")


class _HeadTailStreamReader:
    """跟 _BoundedStreamReader 一样持续排空管道防死锁，但保留两段：
    开头 head_bytes 字节 + 结尾 tail_bytes 字节，中间丢弃（而不是只保留开头）。

    用于 stderr：之前只保留开头的版本有一个真实验证过的缺陷——如果程序先写
    大量内容到 stderr，真正有用的异常/traceback 往往出现在最后，只保留开头
    会把这条真正有用的信息丢掉，保留的反而是前面没什么价值的内容。head+tail
    两段式解决的正是这个问题。

    head 区间和 tail 区间严格不重叠：tail 只从"超出 head 的部分"开始累积，
    所以 head+tail 拼接后不会出现重复内容。
    """

    def __init__(self, stream, head_bytes: int, tail_bytes: int, chunk_size: int = READ_CHUNK_SIZE):
        self._stream = stream
        self._head_limit = head_bytes
        self._tail_limit = tail_bytes
        self._chunk_size = chunk_size
        self._head_buf = bytearray()
        self._tail_buf = bytearray()
        self.total_bytes = 0
        self._thread = threading.Thread(target=self._drain, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _drain(self) -> None:
        try:
            while True:
                chunk = self._stream.read(self._chunk_size)
                if not chunk:
                    break
                self.total_bytes += len(chunk)
                if len(self._head_buf) < self._head_limit:
                    room = self._head_limit - len(self._head_buf)
                    self._head_buf.extend(chunk[:room])
                    chunk = chunk[room:]  # 剩下的部分（如果有）才算进 tail 的候选范围
                if chunk:
                    self._tail_buf.extend(chunk)
                    if len(self._tail_buf) > self._tail_limit:
                        # 只保留最后 tail_limit 字节的滑动窗口
                        del self._tail_buf[: -self._tail_limit]
        except (ValueError, OSError):
            pass

    def join(self, timeout: float | None = None) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    @property
    def truncated(self) -> bool:
        return self.total_bytes > (len(self._head_buf) + len(self._tail_buf))

    def text(self) -> str:
        head_text = self._head_buf.decode("utf-8", errors="replace")
        tail_text = self._tail_buf.decode("utf-8", errors="replace")
        return head_text + tail_text


def _write_stdin_and_close(proc: subprocess.Popen, data: bytes) -> None:
    try:
        proc.stdin.write(data)
    except (BrokenPipeError, OSError):
        # 子进程可能已经因为代码里的早期错误退出，stdin 写入失败不是本函数要处理的错误
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass


def _probe_docker_daemon_reachable() -> bool:
    """`docker run` 以非零码退出（且不是镜像缺失那个125）之后，用这个探测
    判断原因是"daemon 这会儿连不上"还是"容器里的代码自己报错"——判定只看
    这条探测命令自己的退出码（0=成功），不解析任何输出文本，不是在猜字符串。

    只在失败路径上调用（不给每次成功执行都加一次额外开销），超时3秒。

    已知局限（诚实记录，不是已解决）：`docker run` 失败和这次探测之间存在
    时间窗口，如果 daemon 恰好在这个窗口里从不可达恢复到可达，会把本来是
    daemon 问题导致的失败误判成 tool_input_error——这是刻意选择的保守方向
    （宁可少重试，不要把用户代码的真实报错错误地当成基础设施问题去重试），
    不是忽略了这个竞态条件。
    """
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=3,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _kill_and_confirm_removed(container_name: str) -> bool:
    """Best-effort kill of a named container, with a bounded poll to
    confirm it is actually gone afterward.

    Returns True only if removal was directly observed via `docker
    inspect` returning "no such object". Returns False if we cannot
    confirm this within the grace period -- this is NOT the same as
    confirming the kill failed; it only means we refuse to claim
    something we didn't observe. A `docker kill` exit code of 0 only
    means the kill signal was accepted, not that the container has
    actually stopped and been removed yet.
    """
    try:
        subprocess.run(
            ["docker", "kill", container_name],
            capture_output=True,
            text=True,
            timeout=CLEANUP_KILL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        # `docker kill` 本身卡住了，不代表容器没被杀掉——继续往下轮询确认，
        # 用真实观察结果判断，而不是靠这条命令的返回值推断
        pass
    except FileNotFoundError:
        return False

    deadline = time.monotonic() + CLEANUP_CONFIRM_TIMEOUT
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["docker", "inspect", container_name],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
        if result.returncode != 0:
            # "no such object" -- --rm 已经把它连容器对象一起移除了
            return True
        time.sleep(CLEANUP_POLL_INTERVAL)
    return False


def _run_in_container(code: str) -> str:
    """Run `code` to completion inside a fresh, isolated container and
    return captured stdout (or a descriptive error string).

    Blocking by design -- callers must offload this to a thread.
    """
    code_bytes = code.encode("utf-8")
    if len(code_bytes) > MAX_CODE_BYTES:
        return (
            f"Python Error: submitted code is {len(code_bytes)} bytes, "
            f"which exceeds the {MAX_CODE_BYTES}-byte limit. Shorten the "
            "code (this check runs before any container is started)."
        )

    container_name = f"odr-python-executor-{uuid.uuid4().hex[:12]}"
    cmd = [
        "docker", "run", "--rm", "-i",
        "--name", container_name,
        "--network", "none",
        "--read-only",
        "--tmpfs", f"/tmp:size={TMPFS_SIZE},mode=1777",
        f"--memory={MEMORY_LIMIT}",
        f"--memory-swap={MEMORY_LIMIT}",
        f"--cpus={CPU_LIMIT}",
        f"--pids-limit={PIDS_LIMIT}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pull=never",
        IMAGE_NAME,
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        # 2026-10 加入重试分类：这两个分支是"容器/进程根本没能起来"，跟下面
        # proc.wait() 之后"容器起来了但镜像缺失/daemon 拒绝连接"（走 returncode!=0
        # 分支）是不同的失败面——后者目前不分类为瞬时性，因为要可靠区分"这次
        # daemon 连不上"和"别的原因导致非零退出码"需要解析 stderr 文本内容，这正是
        # 要刻意避免的"靠字符串前缀分类"。这里改成抛 TransientToolError 而不是直接
        # return 字符串，只影响 execute_tool_safely 的重试判断路径；下面的公开
        # python_executor 函数会把它转换回和以前完全一样的字符串，直接调用
        # python_executor.ainvoke(...)（现有 18 个测试的调用方式）的行为不受影响。
        raise TransientToolError(
            "Python Error: the `docker` command was not found. "
            "Containerized execution requires Docker to be installed and "
            "on PATH -- this tool has no non-containerized fallback."
        )
    except OSError as e:
        raise TransientToolError(f"Python Error: failed to start the sandbox container: {e}")

    stdout_reader = _BoundedStreamReader(proc.stdout, MAX_OUTPUT_BYTES)
    stderr_reader = _HeadTailStreamReader(proc.stderr, STDERR_HEAD_BYTES, STDERR_TAIL_BYTES)
    stdout_reader.start()
    stderr_reader.start()
    stdin_thread = threading.Thread(
        target=_write_stdin_and_close, args=(proc, code_bytes), daemon=True
    )
    stdin_thread.start()

    try:
        proc.wait(timeout=TIMEOUT_SECONDS)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True

    if timed_out:
        cleanup_confirmed = _kill_and_confirm_removed(container_name)
        # 容器死了之后，docker run 客户端进程自己也该随之退出；给它一点时间，
        # 真退不掉再兜底 kill 一下这个本地客户端进程（不是容器）。
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        stdin_thread.join(timeout=2)
        stdout_reader.join(timeout=READER_JOIN_TIMEOUT)
        stderr_reader.join(timeout=READER_JOIN_TIMEOUT)
        if cleanup_confirmed:
            # 这是"用户代码本身跑太久"的正常超时——干净地确认了容器已清理，
            # 问题出在提交的代码上，不是环境本身出了问题。2026-10 加入
            # failure_kind 分类后：这个分支保持 return 字符串（不抛异常），
            # execute_tool_safely 据此把它归为 tool_input_error，不计入熔断
            # ——模型写的代码跑超时很正常，不该因此被熔断掉整个工具。
            return (
                f"Python Error: execution exceeded the {TIMEOUT_SECONDS}s "
                "timeout; the sandbox container was killed and its removal "
                "was confirmed via `docker inspect`. No output is "
                "available for code that did not finish."
            )
        # 这不是"代码跑太久"的问题——是清理机制本身没能在宽限期内确认清理
        # 结果，这是环境/基础设施层面的不确定状态，不是提交的代码的错。
        # 2026-10 改成抛 TransientToolError（而不是直接 return 字符串），
        # 这样 execute_tool_safely 才能把它归为 infrastructure_error（计入
        # 熔断）而不是 tool_input_error，并且因为是 TransientToolError，
        # 还会被纳入有限次数的重试——每次重试会用一个新的 uuid 命名的容器，
        # 不会和"可能还没真正停止"的旧容器冲突。公开的 python_executor 函数
        # 会把这个异常转换回和以前完全一致的字符串（见下方），
        # test_cleanup_not_confirmed_is_reported_honestly 这个已有测试
        # 断言的是这段消息文本，不是"是否抛异常"，所以不受影响。
        raise TransientToolError(
            f"Python Error: execution exceeded the {TIMEOUT_SECONDS}s "
            "timeout; a kill was issued but the container's removal could "
            "NOT be confirmed within the cleanup grace period -- it may "
            "still be stopping. No output is available."
        )

    stdin_thread.join(timeout=2)
    stdout_reader.join(timeout=READER_JOIN_TIMEOUT)
    stderr_reader.join(timeout=READER_JOIN_TIMEOUT)

    stdout_text = stdout_reader.text()
    stderr_text = stderr_reader.text()

    if proc.returncode != 0:
        # 2026-10 修复：之前这个分支统一按 tool_input_error 处理，因为无法
        # 在不解析 stderr 的前提下区分"容器里的代码自己报错"和"docker run
        # 这个进程本身没能正常工作"（镜像缺失/daemon连不上）。实测（Docker
        # 29.2.1）确认：镜像缺失稳定返回退出码125；但"daemon不可达"和
        # "容器内未捕获异常"**退出码完全相同，都是1**——并不遵循"125=docker
        # 自身错误"这条常被引用的约定，纯退出码在这两者之间天然不可区分。
        # 解法：125 直接按 infrastructure_error 处理（--pull=never 下镜像
        # 缺失不会因为重试而恢复，所以不重试）；其余非零退出码，失败之后
        # 再探测一次 `docker version` 自己的退出码（不解析任何文本）判断
        # daemon 当下是否可达，由此决定分类。
        if proc.returncode == 125:
            raise NonRetryableInfrastructureError(
                f"Python Error: docker run exited with code 125 (Docker's own "
                "error, typically a missing/invalid image). This image is "
                f"never auto-pulled (--pull=never), so retrying will not "
                f"recover from a missing image: {stderr_text.strip()}"
            )
        if proc.returncode != 137 and not _probe_docker_daemon_reachable():
            # 探测失败/超时：daemon 当下连不上，原来的非零退出大概率是这个
            # 原因——分类为可重试的瞬时错误。137(OOM) 不做这个探测，
            # 因为那是我们自己的 --memory 限制杀的容器，和 daemon 是否可达
            # 无关，探测只会白白增加延迟。
            raise TransientToolError(
                f"Python Error: execution failed with exit code "
                f"{proc.returncode}, and a post-failure probe confirmed the "
                "Docker daemon is not reachable right now -- treating this "
                f"as a transient infrastructure issue: {stderr_text.strip()}"
            )
        if proc.returncode == 137:
            # 137 (SIGKILL) 跟超出内存限制一致，但不是内存限制独有的信号——
            # 外部直接 kill -9 这个进程也会得到同样的退出码，不能断言唯一原因
            hint = (
                " (exit 137: consistent with being OOM-killed for exceeding "
                f"the {MEMORY_LIMIT} memory limit, but exit 137 can also "
                "result from an external SIGKILL, so this is not proof of "
                "the specific cause)"
            )
        else:
            hint = ""
        detail = stderr_text.strip() or f"process exited with code {proc.returncode}"
        if stderr_reader.truncated:
            detail += (
                f"\n... [stderr truncated: kept first {STDERR_HEAD_BYTES} + "
                f"last {STDERR_TAIL_BYTES} bytes, omitted the middle, "
                f"{stderr_reader.total_bytes} bytes written in total]"
            )
        return f"Python Error: {detail}{hint}"

    if not stdout_text.strip():
        return "Code executed successfully but produced no printed output. Use print() to see results."

    if stdout_reader.truncated:
        stdout_text += (
            f"\n... [stdout truncated at {MAX_OUTPUT_BYTES} bytes, "
            f"{stdout_reader.total_bytes} bytes written in total]"
        )
    return stdout_text


@tool(description=PYTHON_EXECUTOR_DESCRIPTION)
async def python_executor(code: str) -> str:
    """Execute Python/pandas code inside an isolated Docker container and
    return captured stdout.

    Args:
        code: Python code using pandas (as pd). Must use print() to
              produce visible output.

    Returns:
        Captured stdout, or a descriptive error message if execution
        failed, timed out, or the sandbox itself could not run.
    """
    try:
        return await asyncio.to_thread(_run_in_container, code)
    except (TransientToolError, NonRetryableInfrastructureError) as e:
        # _run_in_container 为了让 execute_tool_safely 能做重试判断，对
        # "容器/进程没能启动"、"镜像缺失"、"探测确认daemon不可达"这些失败抛的
        # 是类型化异常而不是直接 return 字符串；这里原样转换回字符串（str(e)
        # 就是构造异常时传入的那条消息，不做任何改写），直接调用
        # python_executor.ainvoke(...)（现有 18 个测试的调用方式）时行为和
        # 加这层重试判断之前完全一致。
        return str(e)
    except Exception as e:
        return f"Python Error: {type(e).__name__}: {str(e)}"


async def python_executor_tool_call(code: str) -> str:
    """execute_tool_safely 实际调用的入口，而不是上面的公开 python_executor 工具。

    行为和公开工具完全一致，唯一区别是不吞掉 TransientToolError——
    execute_tool_safely 需要看到真实的异常类型才能判断是否值得重试。
    LangChain 通过 `.ainvoke()` 直接调用公开工具时（包括现有测试）走的是
    上面那条会把 TransientToolError 转换回字符串的路径，不受这个函数影响。
    """
    return await asyncio.to_thread(_run_in_container, code)
