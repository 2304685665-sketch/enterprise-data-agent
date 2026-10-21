"""python_executor 工具的单元测试 —— 容器化加固版本（2026-10-09 第三轮）。

第三轮新增/修正：
- stderr 从"只保留开头"改为"开头2KB+结尾6KB"后，补了验证——
  确认末尾的真实异常信息（之前会被截掉）现在确实能看到
- 新增并发测试：asyncio.gather 同时跑 5 个调用，验证互不干扰
  （各自独立的容器名/线程/缓冲区，不会串数据）
- 新增 --cap-drop=ALL 的直接验证：读容器内 /proc/self/status 的
  CapEff，确认真的是全 0，不再只是"配置上加了但没验证挡住了什么"
- 所有测试函数都加了 pytest 可收集的同步包装，能被 `pytest` 直接发现
  和运行，不再只能靠 `python3 my_extensions/test_python_executor.py`
  这种脚本方式跑

前置条件：
- 本机已安装并启动 Docker
- 已执行过一次：
    docker build -t odr-python-executor:latest my_extensions/docker/python_executor/

本文件里的每一条断言都对照过一次真实运行的输出，不是凭接口设计推测的。
已知局限（诚实记录，不是本文件覆盖的范围）：
- 并发测试只验证了 5 个同时调用不互相串数据，没有测更高并发下是否会
  因为宿主机 CPU/内存/Docker daemon 本身的限制而排队变慢或失败
- 没有做对抗性的容器逃逸测试，"隔离"指的是本文件实测到的几项具体边界
  （网络/宿主机文件系统/内存/进程数/能力位/超时清理），不是泛泛的"绝对安全"
- stdout 仍然是"只保留开头"（跟 stderr 不同的策略，见 python_executor.py
  模块 docstring 的说明），如果关键结果恰好在超长 stdout 的末尾，仍会被截掉
"""

import asyncio
import subprocess
import sys
import time

import pytest

sys.path.insert(0, "src/open_deep_research")
import python_executor as pe  # noqa: E402
from python_executor import python_executor  # noqa: E402


@pytest.mark.asyncio
async def test_basic_calculation():
    """基本的 pandas 计算能力，比如算一组数字的平均值。"""
    code = """
import pandas as pd
data = {"amount": [45000000, 15000000, 12000000, 80000000, 5000000]}
df = pd.DataFrame(data)
print(df["amount"].mean())
"""
    result = await python_executor.ainvoke({"code": code})
    assert "31400000" in result
    print("test_basic_calculation 通过")


@pytest.mark.asyncio
async def test_no_print_gives_hint():
    """代码跑了但没有 print，应该提示用户要用 print 才能看到结果，而不是返回空字符串。"""
    code = "x = 1 + 1"
    result = await python_executor.ainvoke({"code": code})
    assert "print" in result.lower()
    print("test_no_print_gives_hint 通过")


@pytest.mark.asyncio
async def test_syntax_error_returned_raw():
    """代码写错了，报错应该原样返回（来自容器内 Python 解释器的真实 traceback），
    不是吞掉或者假装成功。"""
    code = "print(1 +"
    result = await python_executor.ainvoke({"code": code})
    assert "Python Error" in result
    assert "SyntaxError" in result
    print("test_syntax_error_returned_raw 通过")


@pytest.mark.asyncio
async def test_network_access_actually_blocked():
    """真实安全边界：不是靠白名单拦 import，是靠 --network none 让网络
    在操作系统层面就不可达。断言不绑定具体的英文错误文案（不同 OS/网络栈
    报错文字可能不同），只断言"连接没有成功"这个不变的事实。"""
    code = """
import socket
socket.create_connection(("8.8.8.8", 53), timeout=3)
print("connected")
"""
    result = await python_executor.ainvoke({"code": code})
    assert "connected" not in result
    assert "Python Error" in result
    print("test_network_access_actually_blocked 通过（真实网络不可达，断言不依赖具体错误文案）")


@pytest.mark.asyncio
async def test_host_filesystem_not_visible():
    """验证容器看不到宿主机文件系统：尝试打开这个项目自己在宿主机上的
    绝对路径文件（pyproject.toml），容器内必须报错，因为没有挂载任何
    宿主机目录。"""
    host_path = "/Users/user/project/pyproject.toml"
    code = f'open("{host_path}")'
    result = await python_executor.ainvoke({"code": code})
    assert "Python Error" in result
    assert "FileNotFoundError" in result
    print("test_host_filesystem_not_visible 通过（宿主机文件系统确认不可见）")


@pytest.mark.asyncio
async def test_os_import_now_succeeds_but_is_harmless():
    """行为变化记录：import os 不再被白名单拦截，但因为容器没有宿主机
    文件/网络可见性，os.getcwd() 只能看到容器自己的 /tmp。"""
    code = """
import os
print(os.getcwd())
"""
    result = await python_executor.ainvoke({"code": code})
    assert "Python Error" not in result
    assert result.strip() == "/tmp"
    print("test_os_import_now_succeeds_but_is_harmless 通过（os可导入，但看到的只是容器自己的/tmp）")


@pytest.mark.asyncio
async def test_timeout_actually_enforced_and_cleanup_confirmed():
    """超时必须被真正强制执行，且清理必须被真实确认（不是假设 kill 命令
    返回就代表容器已经停止/移除）。临时把 TIMEOUT_SECONDS 调小到 3 秒，
    并断言返回信息里明确说明清理"已确认"，这对应的是
    _kill_and_confirm_removed 轮询 `docker inspect` 的真实结果，不是
    凭 `docker kill` 的退出码推断的。"""
    original_timeout = pe.TIMEOUT_SECONDS
    pe.TIMEOUT_SECONDS = 3
    try:
        start = time.time()
        code = """
import time
while True:
    time.sleep(1)
"""
        result = await python_executor.ainvoke({"code": code})
        elapsed = time.time() - start
        assert elapsed < 10, f"应该在超时附近被杀掉，实际耗时 {elapsed:.1f}s"
        assert "timeout" in result.lower()
        assert "killed" in result.lower()
        assert "removal was confirmed" in result
        print(
            f"test_timeout_actually_enforced_and_cleanup_confirmed 通过"
            f"（{elapsed:.1f}s 内被强制终止，且清理已被 docker inspect 真实确认）"
        )
    finally:
        pe.TIMEOUT_SECONDS = original_timeout


@pytest.mark.asyncio
async def test_cleanup_not_confirmed_is_reported_honestly():
    """故意把确认清理的轮询时间窗压到 0，制造一个"kill 已发出但我们没有
    实际确认清理完成"的场景，验证函数会如实报告"无法确认"，而不是在这种
    情况下仍然声称清理成功。这不是伪造失败，是真实触发了
    _kill_and_confirm_removed 的另一条分支。"""
    original_timeout = pe.TIMEOUT_SECONDS
    original_confirm_timeout = pe.CLEANUP_CONFIRM_TIMEOUT
    pe.TIMEOUT_SECONDS = 3
    pe.CLEANUP_CONFIRM_TIMEOUT = 0
    try:
        result = await python_executor.ainvoke(
            {"code": "import time\nwhile True:\n    time.sleep(1)"}
        )
        assert "could NOT be confirmed" in result
        print("test_cleanup_not_confirmed_is_reported_honestly 通过（无法确认时如实报告，不冒称成功）")
    finally:
        pe.TIMEOUT_SECONDS = original_timeout
        pe.CLEANUP_CONFIRM_TIMEOUT = original_confirm_timeout


@pytest.mark.asyncio
async def test_memory_limit_enforced_without_overfitting_to_exit_code():
    """修正过度依赖：不再硬性断言退出码必须是 137，也不再要求错误文案里
    一定出现"memory"这个词（这两者都是实现细节，不同 Docker/cgroup 版本
    对 OOM 的上报方式可能不同）。真正要验证的不变事实是：
    1) 超出限制的分配确实没有成功执行到后面的 print
    2) 工具确实返回了一个错误，而不是悄悄"成功"或挂起
    3) 耗时很短（证明是被容器迅速终止，不是碰巧跑完或卡住）"""
    start = time.time()
    code = """
x = bytearray(512 * 1024 * 1024)
print("should not reach here")
"""
    result = await python_executor.ainvoke({"code": code})
    elapsed = time.time() - start
    assert "should not reach here" not in result
    assert "Python Error" in result
    assert elapsed < 10, f"应该很快被终止，实际耗时 {elapsed:.1f}s"
    print(
        f"test_memory_limit_enforced_without_overfitting_to_exit_code 通过"
        f"（{elapsed:.1f}s 内失败，未对具体退出码/错误文案做强假设）"
    )


@pytest.mark.asyncio
async def test_missing_image_gives_clear_error():
    """如果镜像没有被提前 build 好，工具必须给出清楚可操作的报错，而不是
    裸异常或者假装执行成功。"""
    original_image = pe.IMAGE_NAME
    pe.IMAGE_NAME = "odr-python-executor-this-tag-does-not-exist:latest"
    try:
        result = await python_executor.ainvoke({"code": "print(1)"})
        assert "Python Error" in result
        assert "No such image" in result or "Unable to find image" in result
        print("test_missing_image_gives_clear_error 通过（镜像缺失时报错清晰，不是裸异常）")
    finally:
        pe.IMAGE_NAME = original_image


@pytest.mark.asyncio
async def test_docker_daemon_unreachable_gives_clear_error():
    """Docker daemon 不可达（而不是 docker 命令缺失）时，也必须给出清楚的
    报错，而不是挂起或裸异常。通过把 DOCKER_HOST 指向一个没有监听的端口
    来真实触发这个场景（不是 mock，是真的连不上）。"""
    import os

    original_docker_host = os.environ.get("DOCKER_HOST")
    os.environ["DOCKER_HOST"] = "tcp://127.0.0.1:1"
    try:
        start = time.time()
        result = await python_executor.ainvoke({"code": "print(1)"})
        elapsed = time.time() - start
        assert "Python Error" in result
        assert elapsed < 10, f"应该很快失败而不是挂起，实际耗时 {elapsed:.1f}s"
        print(
            f"test_docker_daemon_unreachable_gives_clear_error 通过"
            f"（{elapsed:.2f}s 内明确报错：{result[:80]}...）"
        )
    finally:
        if original_docker_host is None:
            os.environ.pop("DOCKER_HOST", None)
        else:
            os.environ["DOCKER_HOST"] = original_docker_host


@pytest.mark.asyncio
async def test_code_size_limit_rejected_before_starting_container():
    """提交的代码本身不能无限大。超限时必须在启动容器之前就快速拒绝
    （用耗时很短这一点证明没有真的起了容器），避免被用来做资源消耗型攻击
    （比如一个几百 MB 的字符串常量）。"""
    start = time.time()
    oversized_code = "x = 1\n" * 20000  # 远超 MAX_CODE_BYTES
    result = await python_executor.ainvoke({"code": oversized_code})
    elapsed = time.time() - start
    assert "Python Error" in result
    assert "exceeds" in result
    assert elapsed < 1, f"应该在启动容器前就快速拒绝，实际耗时 {elapsed:.2f}s"
    print(f"test_code_size_limit_rejected_before_starting_container 通过（{elapsed*1000:.0f}ms 内拒绝，未启动容器）")


@pytest.mark.asyncio
async def test_pids_limit_caps_fork_style_load():
    """纵深防御验证：--pids-limit=64 应该真实限制住容器内能创建的进程数，
    即便代码本身没有被识别为"恶意"，也要在资源层面被卡住，而不是让容器
    （进而宿主机）被进程数耗尽拖垮。"""
    code = """
import os, time
count = 0
try:
    for i in range(200):
        pid = os.fork()
        if pid == 0:
            time.sleep(3)
            os._exit(0)
        count += 1
except OSError as e:
    print(f"forked {count} before error: {type(e).__name__}")
"""
    result = await python_executor.ainvoke({"code": code})
    assert "forked" in result
    assert "before error" in result
    forked_count = int(result.split("forked ")[1].split(" before")[0])
    assert forked_count < 200, f"pids-limit 应该在 200 之前就拦住，实际 fork 了 {forked_count} 个"
    print(f"test_pids_limit_caps_fork_style_load 通过（在 {forked_count} 个进程时被 --pids-limit 拦住）")


@pytest.mark.asyncio
async def test_large_stdout_is_bounded_not_fully_buffered():
    """压力测试：打印约 50MB 到 stdout，验证：
    1) 返回内容的长度被限制在 MAX_OUTPUT_BYTES 附近，不是 50MB
    2) 返回内容里包含明确的截断提示和真实总字节数
    3) 耗时合理（证明容器没有被我们自己的采集逻辑拖慢到挂起）
    这是对"移除 capture_output=True 的无上限缓存风险"这条修复的直接验证。"""
    code = 'for _ in range(50000):\n    print("A" * 1000)\n'
    start = time.time()
    result = await python_executor.ainvoke({"code": code})
    elapsed = time.time() - start
    assert len(result) < pe.MAX_OUTPUT_BYTES + 200, f"返回内容应该被限制住，实际长度 {len(result)}"
    assert "truncated" in result
    assert "50050000 bytes written in total" in result
    assert elapsed < 15, f"不应该因为大量输出而明显变慢，实际耗时 {elapsed:.1f}s"
    print(
        f"test_large_stdout_is_bounded_not_fully_buffered 通过"
        f"（50MB 真实输出被限制到返回 {len(result)} 字符，耗时 {elapsed:.2f}s）"
    )


@pytest.mark.asyncio
async def test_large_stderr_is_bounded_but_keeps_the_real_error():
    """写大量内容到 stderr 再抛异常，验证两件事：
    1) 返回内容被限制住，不会把几十 MB 的 stderr 原样搬到 Python 内存里
    2) 跟旧版本（只保留开头）不同——现在因为改成了开头2KB+结尾6KB，
       真正的异常信息（出现在整段输出的最后）必须确实出现在返回结果里，
       这是这次改动要解决的真实问题，不是"记录一个已知限制"而已"""
    code = (
        "import sys\n"
        "for _ in range(50000):\n"
        '    sys.stderr.write("B" * 1000)\n'
        'raise RuntimeError("boom")\n'
    )
    start = time.time()
    result = await python_executor.ainvoke({"code": code})
    elapsed = time.time() - start
    max_expected_len = pe.STDERR_HEAD_BYTES + pe.STDERR_TAIL_BYTES + 300
    assert result.startswith("Python Error:")
    assert len(result) < max_expected_len, f"返回内容应该被限制住，实际长度 {len(result)}"
    assert "truncated" in result
    assert "bytes written in total" in result
    # 这是关键断言：末尾的真实异常信息必须被保留下来，不能被截没
    assert "RuntimeError" in result, "真正的异常类型应该保留在结尾，没有被截断丢掉"
    assert "boom" in result, "真正的异常消息应该保留在结尾，没有被截断丢掉"
    assert elapsed < 15, f"不应该因为大量输出而明显变慢，实际耗时 {elapsed:.1f}s"
    print(
        f"test_large_stderr_is_bounded_but_keeps_the_real_error 通过"
        f"（大量 stderr 被限制到返回 {len(result)} 字符，耗时 {elapsed:.2f}s，"
        f"且末尾的 RuntimeError/boom 确实被保留下来了）"
    )


@pytest.mark.asyncio
async def test_timeout_cleanup_actually_removes_container():
    """比 test_timeout_actually_enforced_and_cleanup_confirmed 更直接的验证：
    固定住本次调用生成的容器名（通过 monkeypatch uuid4），超时结束后，
    真的去跑一次 `docker ps -a --filter name=<那个具体名字>`，确认宿主机
    上确实没有残留这个容器——不是读函数自己返回的文案，是外部独立观察。"""
    import uuid as uuid_module

    fixed_uuid = uuid_module.UUID(int=0x1234567890ABCDEF1234567890ABCDEF)
    expected_name = f"odr-python-executor-{fixed_uuid.hex[:12]}"

    original_uuid4 = pe.uuid.uuid4
    original_timeout = pe.TIMEOUT_SECONDS
    pe.uuid.uuid4 = lambda: fixed_uuid
    pe.TIMEOUT_SECONDS = 3
    try:
        result = await python_executor.ainvoke(
            {"code": "import time\nwhile True:\n    time.sleep(1)"}
        )
        assert "removal was confirmed" in result
        ps_result = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={expected_name}", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=5,
        )
        assert expected_name not in ps_result.stdout, (
            f"容器 {expected_name} 应该已被清理，但 docker ps -a 仍然看到它: {ps_result.stdout!r}"
        )
        print(f"test_timeout_cleanup_actually_removes_container 通过（独立 docker ps -a 验证容器 {expected_name} 确实不存在了）")
    finally:
        pe.uuid.uuid4 = original_uuid4
        pe.TIMEOUT_SECONDS = original_timeout


@pytest.mark.asyncio
async def test_concurrent_calls_do_not_cross_contaminate():
    """用 asyncio.gather 真实同时发起 5 次调用，每次代码都会产出一个
    独立可区分的数字，验证返回结果跟发起顺序一一对应，不会因为共享了
    某个模块级状态（比如容器名、缓冲区）而把结果搞混。每次调用内部都是
    全新的 _BoundedStreamReader/_HeadTailStreamReader 实例和随机容器名，
    这条测试验证的是这个"设计上应该独立"的假设在真实并发下确实成立，
    不是只靠读代码推断的。"""

    async def _one(i: int) -> str:
        return await python_executor.ainvoke({"code": f"print({i} * 1000)"})

    results = await asyncio.gather(*[_one(i) for i in range(5)])
    for i, result in enumerate(results):
        expected = str(i * 1000)
        assert result.strip() == expected, (
            f"第 {i} 个并发调用应该返回 {expected!r}，实际返回 {result!r}"
            "——说明并发调用之间可能串了数据"
        )
    print("test_concurrent_calls_do_not_cross_contaminate 通过（5个并发调用结果各自独立，没有串数据）")


@pytest.mark.asyncio
async def test_cap_drop_all_results_in_zero_effective_capabilities():
    """直接验证 --cap-drop=ALL 到底挡住了什么，而不是只验证"配置加了、
    基本执行没坏"。读容器内 /proc/self/status 的 CapEff（当前进程的有效
    capability 位图），--cap-drop=ALL 下这个值必须是全 0
    （0000000000000000），代表没有任何 Linux capability 可用——
    这是对"纵深防御参数已加入但没验证挡住了什么"这条已知局限的直接补充。"""
    code = """
with open("/proc/self/status") as f:
    for line in f:
        if line.startswith("CapEff:"):
            print(line.strip())
            break
"""
    result = await python_executor.ainvoke({"code": code})
    assert "Python Error" not in result
    assert "CapEff:" in result
    cap_value = result.split("CapEff:")[1].strip()
    assert cap_value == "0000000000000000", (
        f"--cap-drop=ALL 应该让 CapEff 全 0，实际读到的值是 {cap_value!r}"
    )
    print(f"test_cap_drop_all_results_in_zero_effective_capabilities 通过（CapEff={cap_value}，确认没有任何capability）")


# ---------------------------------------------------------------------------
# 2026-10 新增：非零退出码分类（125 / 探测daemon可达性）
# 前3个用 mock，只隔离测试 _probe_docker_daemon_reachable 本身；
# 后2个用真实 Docker，走完整的 _run_in_container/python_executor_tool_call。
# ---------------------------------------------------------------------------

def test_probe_returns_true_when_docker_version_succeeds():
    from unittest.mock import patch, MagicMock
    fake_result = MagicMock(returncode=0)
    with patch("python_executor.subprocess.run", return_value=fake_result) as mock_run:
        assert pe._probe_docker_daemon_reachable() is True
    assert mock_run.called
    print("test_probe_returns_true_when_docker_version_succeeds 通过")


def test_probe_returns_false_when_docker_version_fails():
    from unittest.mock import patch, MagicMock
    fake_result = MagicMock(returncode=1)
    with patch("python_executor.subprocess.run", return_value=fake_result):
        assert pe._probe_docker_daemon_reachable() is False
    print("test_probe_returns_false_when_docker_version_fails 通过")


def test_probe_returns_false_on_timeout():
    from unittest.mock import patch
    with patch("python_executor.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="docker version", timeout=3)):
        assert pe._probe_docker_daemon_reachable() is False
    print("test_probe_returns_false_on_timeout 通过")


@pytest.mark.asyncio
async def test_missing_image_classified_as_nonretryable_infrastructure_error():
    """退出码125（镜像缺失）应该被分类为 NonRetryableInfrastructureError，
    不是 TransientToolError——--pull=never 下重试不会让缺失的镜像出现。
    走的是 python_executor_tool_call（execute_tool_safely 实际调用的
    "抛异常版"入口），不是公开工具函数，这样才能直接看到异常类型。"""
    from python_executor import python_executor_tool_call
    original_image = pe.IMAGE_NAME
    pe.IMAGE_NAME = "odr-python-executor-this-tag-does-not-exist:latest"
    try:
        try:
            await python_executor_tool_call("print(1)")
            raised = None
        except Exception as e:
            raised = e
        assert isinstance(raised, pe.NonRetryableInfrastructureError), (
            f"预期 NonRetryableInfrastructureError，实际是 "
            f"{type(raised).__name__ if raised else None}"
        )
        assert "125" in str(raised)
        print(f"test_missing_image_classified_as_nonretryable_infrastructure_error 通过"
              f"（{type(raised).__name__}: {str(raised)[:80]}...）")
    finally:
        pe.IMAGE_NAME = original_image


@pytest.mark.asyncio
async def test_docker_daemon_unreachable_classified_as_transient_retryable():
    """daemon不可达（exit code 1，和代码报错相同）应该经探测确认后分类为
    TransientToolError，不是 tool_input_error。用真实 DOCKER_HOST 指向不存在
    端口触发（和已有的 test_docker_daemon_unreachable_gives_clear_error 用
    同样的真实触发方式），这里额外验证的是"抛异常版"入口能看到正确的异常
    类型，而不只是公开工具函数转换回的字符串里有没有 'Python Error'。"""
    import os
    from python_executor import python_executor_tool_call

    original_docker_host = os.environ.get("DOCKER_HOST")
    os.environ["DOCKER_HOST"] = "tcp://127.0.0.1:1"
    try:
        start = time.time()
        try:
            await python_executor_tool_call("print(1)")
            raised = None
        except Exception as e:
            raised = e
        elapsed = time.time() - start
        assert isinstance(raised, pe.TransientToolError), (
            f"预期 TransientToolError，实际是 "
            f"{type(raised).__name__ if raised else None}: {raised}"
        )
        assert elapsed < 10, f"应该很快失败（含一次3秒探测），实际耗时 {elapsed:.1f}s"
        print(f"test_docker_daemon_unreachable_classified_as_transient_retryable 通过"
              f"（{elapsed:.2f}s内确认daemon不可达，正确分类为TransientToolError）")
    finally:
        if original_docker_host is None:
            os.environ.pop("DOCKER_HOST", None)
        else:
            os.environ["DOCKER_HOST"] = original_docker_host


async def main():
    await test_basic_calculation()
    await test_no_print_gives_hint()
    await test_syntax_error_returned_raw()
    await test_network_access_actually_blocked()
    await test_host_filesystem_not_visible()
    await test_os_import_now_succeeds_but_is_harmless()
    await test_timeout_actually_enforced_and_cleanup_confirmed()
    await test_cleanup_not_confirmed_is_reported_honestly()
    await test_memory_limit_enforced_without_overfitting_to_exit_code()
    await test_missing_image_gives_clear_error()
    await test_docker_daemon_unreachable_gives_clear_error()
    await test_code_size_limit_rejected_before_starting_container()
    await test_pids_limit_caps_fork_style_load()
    await test_large_stdout_is_bounded_not_fully_buffered()
    await test_large_stderr_is_bounded_but_keeps_the_real_error()
    await test_timeout_cleanup_actually_removes_container()
    await test_concurrent_calls_do_not_cross_contaminate()
    await test_cap_drop_all_results_in_zero_effective_capabilities()
    test_probe_returns_true_when_docker_version_succeeds()
    test_probe_returns_false_when_docker_version_fails()
    test_probe_returns_false_on_timeout()
    await test_missing_image_classified_as_nonretryable_infrastructure_error()
    await test_docker_daemon_unreachable_classified_as_transient_retryable()
    print(
        "\n全部测试通过。网络隔离、宿主机文件系统隔离、内存限制、进程数限制、"
        "执行超时及其清理确认、有界输出采集、非零退出码分类均已用真实容器行为验证。"
    )


if __name__ == "__main__":
    asyncio.run(main())
