# 项目改进清单（已发现、待办、已修复）

最后更新：2026-10-09

## 已完成

### sql_executor 真实安全漏洞：ATTACH DATABASE 绕过只读保护（2026-10-09，已修复并反向验证）

- **状态**：已修复，已用反向验证确认新测试真的能检测到这个漏洞，未提交（改动在工作区，等待确认后再提交）
- **发现方式**：在对 `sql_executor.py` 的历次审查中，之前的审查结论写的是"ATTACH 被上层字符串分类器拒绝，不会到达数据库层"，并据此认为"即使字符串检查失效，mode=ro 也是真正的安全边界"这条结论对 ATTACH 同样适用——但这个结论从未被直接验证过，只验证了"经过 `sql_executor` 这一层会被拒绝"。真正的验证方式是绕开分类器，直接在一个独立的临时目录里调用 `se._run_query_sync("ATTACH DATABASE 'evil.db' AS e")`：
  ```
  result: Query executed successfully but returned no rows.
  evil.db exists: True
  ```
  这证明此前"mode=ro 是真正的数据库层安全边界"这条结论对 ATTACH 这种攻击方式是不准确的——审查方法本身有缺口：只测了"经过上层分类器"的路径，没有测"假设上层完全失效，数据库层自己扛不扛得住"这个真正要验证的问题。
- **为什么 `mode=ro` 单独挡不住 ATTACH**：`mode=ro` 是传给 `sqlite3.connect()` 的 URI 参数，只约束"这一个连接对象"打开主数据库文件的方式。`ATTACH DATABASE 'evil.db' AS e` 在 SQLite 里是在同一个连接下**额外打开一个全新的、独立的数据库文件**，这个新文件的打开方式不受连接最初 `mode=ro` 这个参数的约束——相当于 mode=ro 管得住"前门"，但 ATTACH 开的是"后门"，走的是完全不同的路径。额外验证过：单独加 `PRAGMA query_only = ON`（不装下面的 authorizer）也挡不住 ATTACH，`query_only` 只拦截针对（主/已挂载）数据库的写 SQL，不拦截 ATTACH/DETACH 这两个命令本身。
- **修复方式**：`src/open_deep_research/sql_executor.py` 新增 `_deny_attach_and_detach(action, arg1, arg2, dbname, source)` authorizer 回调，通过 `conn.set_authorizer(_deny_attach_and_detach)` 注册——对 `sqlite3.SQLITE_ATTACH`/`sqlite3.SQLITE_DETACH` 返回 `sqlite3.SQLITE_DENY`，其余动作返回 `sqlite3.SQLITE_OK`。这一层在 `cur.execute(query)` 之前完成注册。同时保留/新增 `conn.execute("PRAGMA query_only = ON")` 作为额外的纵深防御（拦截写 SQL，不是这次漏洞的直接修复点，但仍然值得保留）。`_run_query_sync` 的异常捕获从只捕获 `sqlite3.OperationalError` 放宽为 `sqlite3.Error`，因为 authorizer 拒绝时 Python sqlite3 模块抛出的是 `sqlite3.DatabaseError`（`OperationalError` 的父类，不会被原来的窄 except 捕获到）。
- **反向验证（证明新测试真的有检测能力，不是摆设）**：
  1. 临时把 `conn.set_authorizer(_deny_attach_and_detach)` 这一行注释掉
  2. 用一个隔离运行器单独跑 `test_attach_blocked_at_db_layer`（不触发整个测试文件末尾的 `asyncio.run(main())`），**实测确认测试失败**：`AssertionError: 应该以 SQL Error 开头，实际: 'Query executed successfully but returned no rows.'`——失败原因明确证明是 ATTACH 没有被拒绝，不是测试代码本身的问题
  3. 确认临时目录在断言失败之后依然被 `finally` 块正确清理（`find /tmp -iname evil.db` 无结果）
  4. 恢复 `conn.set_authorizer(_deny_attach_and_detach)` 这一行（已用 `grep` 确认代码恢复成原样，没有残留任何"TEMP-DISABLED"之类的标记），**实测确认测试重新通过**
- **最终回归结果（真实运行，非估计）**：`test_sql_executor.py` **20/20 通过**（原19项 + 本次新增 `test_attach_blocked_at_db_layer`）；`test_sql_reliability.py` **3/3 通过**；`git diff --check` 无输出；`demo.db` 确认零改动
- **结论的边界（不要误读）**：这次修复解决的是"ATTACH/DETACH 创建或访问额外数据库文件"这一条具体路径，**不代表"所有可能的 SQL 安全问题均已排除"**。SQLite 的攻击面远不止 ATTACH 这一种（历史上的审查已经分别排除过布尔盲注式的多语句堆叠、注释绕过、CTE写操作伪装、`load_extension`、`writefile`等路径，且都是逐条独立验证的，不是一次性"证明了安全"）。每一轮审查只能证明"这一轮测过的攻击面是安全的"，不能证明"再也没有其他未知的攻击面"——这条边界本身也适用于本次修复。

## 已完成（此前记录）

### python_executor 容器化第三轮加固（2026-10-09，审查反馈，已逐一实测验证）

- **状态**：已实现，已用 `pytest` 和脚本两种方式各跑过一次，18/18 全部真实通过
- 这一轮是对第二轮加固的进一步审查反馈，改了 5 件事：
  1. **确认 docker run 路径不阻塞事件循环**：`_run_in_container` 是纯同步函数（`src/open_deep_research/python_executor.py:299`），所有阻塞操作都在其内部；唯一的卸载点是 `python_executor` 异步工具函数里的 `return await asyncio.to_thread(_run_in_container, code)`（同文件 `:439`）。全文件 `grep` 确认零处使用 `asyncio.create_subprocess_exec`——这是有意选择"同步代码整体丢进线程池"而不是"原生异步子进程API"的设计，没有发现阻塞事件循环的代码路径。
  2. **stderr 改为 头2KB+尾6KB**（新增 `_HeadTailStreamReader` 类）：之前 stderr 只保留开头，如果大量输出之后才出现真正的异常信息会被截掉；新方案 head/tail 两个区间严格不重叠，中间丢弃。**实测证据**：构造"先写50MB到stderr再raise RuntimeError('boom')"的代码，旧方案下 `boom` 会被截没，新方案下返回结果里确认能看到完整的 `RuntimeError: boom`（返回长度 8316 字符，约等于 2048+6144+提示文字）。stdout 没有改，仍然是只保留开头 20000 字节（两种流用不同策略是有意的，不是遗漏，原因见 `python_executor.py` 模块 docstring）。
  3. **新增并发测试**：`asyncio.gather` 同时发起 5 次调用，每次返回一个可区分的数字，断言返回顺序和结果一一对应。**实测通过**，没有发现因为共享模块级状态而串数据的问题（每次调用确实都是独立的容器名、独立的 reader 线程实例）。
  4. **新增 CapEff 验证**：之前"`--cap-drop=ALL` 已加入"只验证了"不影响正常执行"，没有验证它到底挡住了什么。这次直接在容器内读 `/proc/self/status` 的 `CapEff`，**实测确认是 `0000000000000000`**（16个十六进制0，代表没有任何有效 capability），不再只是配置层面的声明。
  5. **测试改为可被 pytest 收集运行**：给每个 `async def test_xxx` 加了 `@pytest.mark.asyncio` 装饰器（项目已有 `pytest-asyncio` 依赖，`asyncio_mode` 是默认的 strict，需要显式标记）；保留了原来 `python3 my_extensions/test_python_executor.py` 脚本直跑的入口（`main()` 仍按顺序 `await` 每个测试函数本身，不受装饰器影响）。**实测**：`uv run pytest my_extensions/test_python_executor.py -v` 收集到 18 个测试，18 passed。
- **这一轮改动的文件**：仅 `src/open_deep_research/python_executor.py`（新增 `_HeadTailStreamReader` 类、新增 `STDERR_HEAD_BYTES`/`STDERR_TAIL_BYTES` 常量、stderr 读取器替换）和 `my_extensions/test_python_executor.py`（补两个新测试、改写大 stderr 测试、全部测试加 pytest 标记）。`Dockerfile` 未改动。
- **仍未覆盖（诚实记录）**：并发测试只验证了5个同时调用不串数据，没有测更高并发量下是否会因为宿主机资源或 Docker daemon 本身的限制而排队/失败；`--cap-drop=ALL` 现在验证了"确实把能力位清零"，但仍然没有验证这具体挡住了哪个真实攻击路径（当前镜像没有可用于演示这类攻击的攻击面）。

### python_executor 容器化（2026-10-09，已用真实容器运行验证，非假设性设计）

- **状态**：已实现并已提交（随第三轮加固一起提交），下方每一条都是真实跑过一次容器得到的结果，不是按接口设计推测的
- **背景**：下方"python_executor 安全边界 —— 架构性天花板"条目（2026-09-18）已经把结论写死了——Python 应用层的 `__builtins__`/`__import__` 白名单挡不住 `pd.__builtins__` 泄漏和 `__subclasses__()` 枚举这两条逃逸路径，这是 Python 语言本身图灵完备性质决定的天花板，"唯一可靠的办法是换成操作系统级别的隔离（容器）"，当时明确写的是路线图待办，不是已完成项。这次就是把这条路线图待办真正实现。
- **改动文件**：
  - 新增 `my_extensions/docker/python_executor/Dockerfile`（最小 `python:3.11-slim` + pandas，非 root 用户，ENTRYPOINT 从 stdin 读代码）
  - 重写 `src/open_deep_research/python_executor.py`：删除旧的 `ALLOWED_MODULES`/`restricted_import`/`safe_builtins` 白名单机制，改为每次调用起一个一次性容器执行代码
  - 重写 `my_extensions/test_python_executor.py`：旧的"验证白名单拦截 os"的测试已不适用（os 现在可以被 import，这是预期行为变化，不是回归），换成针对容器隔离边界本身的测试
- **真实隔离机制**（`docker run` 参数）：`--network none` + `--read-only --tmpfs /tmp` + `--memory=256m --memory-swap=256m`（两者都要设，见下方实测发现）+ `--cpus=0.5` + `--pull=never`（禁止隐式联网拉取镜像）+ 不挂载任何宿主机目录 + 具名容器配合 `docker kill` 实现超时强制终止
- **实测过程中发现的一个真实坑**：最初只设了 `--memory=256m`，在该限制下分配 1GB 的 `bytearray` 竟然"成功"了——因为 Docker 默认允许 swap 到约 2 倍内存，不单独设 `--memory-swap` 的话内存限制形同虚设。补上 `--memory-swap=256m` 之后，同样的分配立刻被 OOM-kill（exit 137）。这条记录下来是因为它不是设计阶段能想到的坑，是动手测才发现的。
- **已用真实运行验证的边界**（`my_extensions/test_python_executor.py`，`uv run python3 my_extensions/test_python_executor.py` 全部通过）：
  - 基础 pandas 计算、无 print 提示、语法错误原样返回 —— 行为与容器化之前一致
  - **网络隔离**：容器内 `socket.create_connection` 到 `8.8.8.8:53` 真实抛出 `OSError: [Errno 101] Network is unreachable`，不是代码层拦截
  - **宿主机文件系统隔离**：容器内尝试 `open()` 这个项目在宿主机上的真实绝对路径，得到 `FileNotFoundError`，确认未挂载任何宿主机目录
  - **os 模块可以被 import，但看到的只是容器自己的环境**：`os.getcwd()` 返回的是容器内的 `/tmp`，不是宿主机路径——这是与旧版本的行为差异（旧版本会在 `__import__` 层拦截并报错），记录为"预期变化"而非"新漏洞"
  - **超时真正被强制执行**：一个 `while True: time.sleep(1)` 死循环，在限制为 3 秒时实测 3.1 秒内被 `docker kill` 杀掉并返回明确的超时错误——这是对旧版本"没有超时限制，死循环会一直占用资源"这条已知缺口的真实修复，不是声明修复
  - **内存限制真正被强制执行**：256m 限制下分配 512MB，实测被 OOM-kill（exit 137），返回明确错误，而不是悄悄"成功"
  - **镜像缺失时报错清晰**：故意把镜像 tag 改成不存在的名字，确认返回的是 docker 原始的 `No such image` 错误文本，不是裸异常或静默失败
- **新增的运维依赖（诚实记录，不是遗漏）**：
  - 本机必须安装并运行 Docker——这是全新的硬性依赖，之前的 `exec()` 方案不需要
  - 镜像必须提前手动 build 好（`docker build -t odr-python-executor:latest my_extensions/docker/python_executor/`），工具本身不会自动 build，`--pull=never` 也刻意禁止了运行时隐式拉取
  - 每次调用多了约 0.3-1 秒的容器启动延迟，这是用安全性换来的代价
- **没有覆盖的部分（诚实记录，不是本次任务范围）**：没有限制容器内 PID 数、没有自定义 seccomp profile、没有做真正的多租户资源隔离（比如多个并发调用之间没有做额外的 CPU/IO 配额隔离，只是各自有自己的 `--cpus`/`--memory` 上限）；这些比 P0-1/P0-2 优先级更低，暂未处理
- **与旧条目的关系**：下方"python_executor 安全边界 —— 架构性天花板"条目的 P0-1/P0-2/P0-3 三点，均已通过本次容器化方案解决，原文保留作为问题发现过程的记录，不做删除

### python_executor 容器化第二轮加固（2026-10-09，审查发现的 5 类问题，已逐一实测验证）

- **状态**：已实现并已提交（随第三轮加固一起提交）。这一轮是对上面"python_executor 容器化"条目的审查反馈——初版容器化解决了网络/文件系统/内存/CPU 隔离，但審查指出了几个更细的工程问题：输出采集方式本身仍有无界内存风险、超时清理没有真正确认、缺少几项常见的纵深防御参数、部分测试对实现细节（退出码、英文错误文案）过度绑定。
- **改动文件**：`src/open_deep_research/python_executor.py`（重写核心执行逻辑）、`my_extensions/test_python_executor.py`（重写并扩充测试）。`Dockerfile` 本身未改动——这一轮全部是 `docker run` 参数和宿主机侧采集逻辑的调整，不需要改镜像。
- **问题1：有界输出采集（已修复）**
  - 原实现用 `subprocess.run(capture_output=True)`，会把子进程全部 stdout/stderr 无上限地攒进内存后再截断字符串——一个打印几十 MB 的循环会先把几十 MB 都搬进 Python 进程内存，截断只是"事后"的字符串切片，不能真正防止内存被占满。
  - 改为 `subprocess.Popen` + 两个后台线程（`_BoundedStreamReader`）分别持续排空 stdout/stderr：每次 `read()` 一个 4096 字节的 chunk，按字节计数，超过 `MAX_OUTPUT_BYTES`（20000）后不再往缓冲区追加，但**仍然继续读取并丢弃**直到 EOF——这是为了不让子进程因为管道写满而阻塞死锁，不是"读够了就不读了"。解码在全部读完后一次性做，用 `errors="replace"` 处理截断点可能切在多字节 UTF-8 字符中间的情况。
  - **实测证据**：构造打印约 50MB（50050000 字节）到 stdout 的代码，返回结果被限制到 20071 字符（含截断提示），耗时 0.59 秒；stderr 同理（50000092 字节被限制到 20085 字符）。两条测试都是真实跑出来的数字，不是推算的。
  - **已知取舍（诚实记录）**：截断保留的是每个流的**开头**部分。如果一段输出先打印大量内容、真正有用的报错信息在最后才出现（比如本次 stderr 压力测试里的 `RuntimeError("boom")`），这条有用信息会被截掉，返回的只是开头的无意义内容。这是"保头不保尾"的已知限制，没有在本轮解决（解决需要环形缓冲/双向截断，复杂度明显上升，暂判定为优先级不够高）。
- **问题2：超时与清理（已修复）**
  - 原实现里，`docker kill` 命令本身没有超时，且一旦 `docker kill` 返回就直接假定容器已经停止——但 CLI 命令返回成功只代表 kill 信号被 daemon 接受了，不代表容器进程已经真正退出、`--rm` 已经真正把容器对象移除。
  - 改为：`docker kill` 本身带 5 秒超时（`CLEANUP_KILL_TIMEOUT`）；之后用 `docker inspect` 做有限轮询（最多 3 秒，`CLEANUP_CONFIRM_TIMEOUT`，每 200ms 一次），只有轮询到 "no such object" 才认为清理被真实确认；轮询超时仍未确认时，返回的错误信息会如实说"could NOT be confirmed"，不会声称清理成功。
  - **实测证据**：
    - 正常情况下，`docker kill` 之后几乎立刻（约 100ms 级别）就能被 `docker inspect` 确认为"不存在"，说明 `--rm` 的清理延迟通常很小，但轮询仍然是必要的保险而不是形式主义。
    - 独立验证（`test_timeout_cleanup_actually_removes_container`）：通过 monkeypatch `uuid.uuid4()` 固定住本次调用生成的容器名，超时之后额外跑一次独立的 `docker ps -a --filter name=<固定名字>`，确认宿主机上真的没有残留——这是跳出被测函数本身、从外部独立观察的验证，不是读函数自己返回的文案。
    - 故意把 `CLEANUP_CONFIRM_TIMEOUT` 压到 0 来触发"无法确认"分支（`test_cleanup_not_confirmed_is_reported_honestly`），确认此时返回的文案确实是"无法确认"而不是冒称成功——这条测试验证的是诚实失败路径本身是真实存在且可达的，不是靠文档宣称。
- **问题3：纵深防御参数（已加入，附兼容性说明）**
  - 新增 `--pids-limit=64`：限制容器内进程/线程总数。**实测**：一个尝试 `os.fork()` 200 次的循环，在第 63 个子进程后收到 `OSError: [Errno 11] Resource temporarily unavailable`，证明限制真实生效，不是摆设。**兼容性风险**：如果未来有需要创建大量子进程/线程的合法用例（本项目目前没有），会被此限制挡住，需要相应调高数值。
  - 新增 `--cap-drop=ALL`：丢弃所有 Linux capabilities（如 `CAP_NET_RAW`/`CAP_SYS_ADMIN`）。**实测**：基础 pandas 计算在此限制下正常执行，无影响。**兼容性风险**：如果将来镜像里需要任何特权操作（比如 ping 需要 `CAP_NET_RAW`、修改文件属主需要 `CAP_CHOWN`），会直接失败——当前只跑纯 Python/pandas 计算，没有这类需求。
  - 新增 `--security-opt=no-new-privileges`：禁止通过 setuid/setgid 二进制提权。**兼容性风险**：几乎为零，镜像里没有任何 setuid 程序，这条防的是假设性的、本次没有复现的攻击路径。
  - `--tmpfs /tmp` 补充显式大小 `size=64m,mode=1777`（之前没有限定大小，默认可能是宿主机内存的较大比例）。**实测**：带显式大小参数的 tmpfs 挂载语法本身可用，基础执行不受影响。
  - **按要求没有改动**：保留 Docker 默认 seccomp profile，没有传 `--security-opt seccomp=unconfined`，也没有写自定义 profile。
  - **这一轮新增参数没有做的事**：没有做大规模的兼容性回归（只验证了"不影响当前唯一的 pandas 计算用例"），也没有做破坏性的权限提升尝试去验证 `cap-drop`/`no-new-privileges` 真的挡住了什么（因为当前镜像里没有可以用来验证这类防御的攻击面，比如没有 setuid 二进制），这两项目前只能算"配置上已加"，不能算"已验证真的挡住了什么具体攻击"。
- **问题4：测试过度依赖实现细节（已修正）**
  - 内存测试：不再硬性要求退出码必须是 137，也不再要求错误文案包含"memory"一词——改为只断言"预期的成功标记没有出现"+"确实返回了错误"+"耗时很短"，这三条才是真正不变的事实，具体的 OOM 上报机制可能随 Docker/cgroup 版本变化。
  - 网络测试：不再断言必须出现"Network is unreachable"这句具体英文——改为只断言"连接未成功"+"返回了错误"，避免测试绑死在某个内核/网络栈的具体报错文案上。
  - 代码里对应地把 exit 137 的解释措辞也软化为"consistent with…but not proof of…"，不再直接声称"肯定是因为超内存"。
- **问题5：其他健壮性（已检查/已实现）**
  - Docker daemon 不可达：真实把 `DOCKER_HOST` 指向一个没有监听的端口（不是 mock），确认 0.03 秒内就能拿到明确的 `Cannot connect to the Docker daemon...` 错误，不会挂起。
  - 镜像缺失：沿用第一轮的验证，确认报错清晰。
  - 清理失败场景：见问题2，已有专门测试覆盖"无法确认"这条路径。
  - 新增代码体积上限 `MAX_CODE_BYTES`（64KB）：超限时在**启动容器之前**就直接拒绝，实测 1 毫秒内返回，没有真的起容器——防止用一段几十/几百 MB 的超大代码字符串做资源消耗型滥用。
  - 按要求没有引入宿主机执行回退，没有挂载任何宿主机目录，没有挂载 Docker socket——这几点在改动前后都成立，这里只是重新确认一次。
- **诚实的最终措辞**：以上这些参数和机制降低了特定风险类别（资源耗尽、输出内存膨胀、清理状态不明）的发生概率和影响范围，**不构成"Docker 容器是绝对安全边界"的证明**——没有做过内核级漏洞的对抗性测试，历史上 Docker/runc 本身也出现过真实的容器逃逸 CVE。`python_executor.py` 顶部的模块 docstring 已经把这句措辞写进去了，避免代码注释和这份文档的说法不一致。

### DataAnalyst 角色路由（Prompt 层面）
- **状态**：已完成，已提交（commit `0c8c6c4`），已用真实测试验证
- **实现方式**：这套架构下 `ConductResearch` 只有一个字符串字段 `research_topic`，没有结构化的角色类型字段，因此"角色路由"只能通过 Prompt 措辞体现，不是代码结构层面的独立子图
- **具体改动**：`research_system_prompt` 加入 sql_executor/python_executor 的工具说明；`lead_researcher_prompt` 加入"内部数据类任务需在 research_topic 中显式点名工具"的指令
- **验证证据**：真实运行中，Supervisor 的 think_tool 反思文字明确写出"requiring use of sql_executor and python_executor"，并且能看到完整的 python_executor 执行代码原文（`statistics.pstdev(...)`）
- **证据强度的边界**：反思文字证明的是"计划"，代码原文证明的是"内容"，但两者与"真实执行结果"之间尚未做结构化关联（比如把某次 tool_calls 和对应的 ToolMessage 直接对应起来）——如果被追问"你怎么证明这份代码真的被执行了"，诚实的回答是：间接证据（最终数字精确匹配、Sources 列表点名 pandas/statistics）+ 反思文字的过程描述叠加支撑，不是结构化断言

### P1：async def 包裹同步阻塞调用
- **状态**：已修复，已用 13 条现有测试回归验证通过
- **问题**：`sql_executor`/`python_executor` 用 `async def` 声明，但内部调用 `sqlite3`/`exec()` 是同步阻塞的，会占用主事件循环、影响其他并发请求
- **发现方式**：Claude Code 独立静态代码审查发现（不是自己写代码时发现的）
- **修复方式**：用 `asyncio.to_thread()` 把同步逻辑包进独立线程执行

## 已发现，待验证/待修复

### 报告中的统计定义与代码不一致
- **代码证据**：`statistics.pstdev(...)`，采用总体标准差（分母为 N）
- **报告问题**：报告文字描述的公式是样本标准差（分母为 N-1）
- **影响**：读者可能无法判断报告里的文字描述和实际计算是否一致
- **建议改进**：报告生成时应明确标注用的是总体还是样本标准差；增加"实际计算方法"与"报告文字描述"的一致性校验
- **注意**：只能确认"报告文字"和"代码"这两处不一致，不能断言这个不一致具体发生在模型的哪个内部环节

### Supervisor 派发时的停止原因丢失
- **发现来源**：精读 `researcher_tools`/`compress_research` 源码确认
- **问题**：`tool_call_iterations`（记录 Researcher 是否因撞上限被迫中断）从未被读取、也从未传给 Supervisor，`compressed_research` 只包含摘要文字
- **影响**：Supervisor 无法区分"Researcher 主动完成"还是"被迫中断"，只能凭摘要内容长短判断信息够不够
- **建议改进**：给 `compress_research` 返回值增加结构化字段 `stop_reason`（如 `completed` / `iteration_limit_reached`）

### Supervisor 并行判断门槛偏保守
- **发现来源**：真实运行观察——明显可拆分的多维度问题，Supervisor 仍只派发单个 Researcher
- **原因**：`lead_researcher_prompt` 里 "Bias towards single agent" 这条策略门槛较高
- **建议改进**：调低判断门槛，做一次并行 vs 串行的对照实验（Token/耗时/准确率）

### 最终报告语言不受控（观察到一次，未复现验证）
- **现象**：英文提问，最终报告是纯中文生成的
- **根本原因**：未确认，怀疑是 `final_report_generation_prompt` 没有强制约束输出语言
- **建议改进**：Prompt 里加一句明确要求语言与提问一致

### python_executor 安全边界（P2，Claude Code 已提示）
- **已覆盖**：`__import__` 白名单限制（pandas/math/statistics），已用测试验证 os 访问被拦住
- **未覆盖**：模块白名单不等于安全沙箱——没有内存/CPU/执行时间限制，没有真正的进程隔离；`asyncio.to_thread()` 只解决了阻塞事件循环的问题，不提供资源隔离
- **建议改进**：生产化前需要接入真正的沙箱（Docker容器、E2B等）

### sql_executor 安全边界 —— P0-4 已修复（2026-09-18 第二轮审查）
- **已修复**：Claude Code 用真实脚本实测确认，`PRAGMA user_version=999`/`PRAGMA application_id=1337`/`PRAGMA journal_mode=WAL` 这几类语句能在单条语句内完成对数据库文件的持久性写入，绕过了"只读"的设计假设。修复方式：把允许前缀从 select/pragma 收紧为只允许 select，已用新增的 `test_pragma_now_rejected` 测试验证
- **缓解因素（同样实测确认，不需要修）**：sqlite3 一次只能执行一条语句，且每次调用都是全新连接，`writable_schema` 这类"需要配合下一条语句才能造成实质破坏"的 PRAGMA，在当前架构下被动挡住了一部分利用链
- **仍未覆盖**：注释前缀绕过检测、数据库文件缺失时的静默失败、并发访问同一 SQLite 文件
- **附带发现（功能性小 bug，非安全问题）**：合法的只读 `WITH ... SELECT`（CTE 写法）会被现有前缀检查误判拒绝，因为语句以 "with" 开头，不是 "select"——记录为已知限制，暂不修

### python_executor 安全边界 —— 架构性天花板（2026-09-18 第二轮审查，Claude Code 实测确认）
- **后续状态（2026-10-09）**：P0-1/P0-2/P0-3 均已通过容器化方案解决，见本文件最上方"python_executor 容器化"条目。本条目原文保留，作为问题发现过程的记录。
- **P0-3 已修复（工具描述层面）**：原描述写"no file system or network access"，Claude Code 实测 `pd.read_pickle`/`pd.read_csv` 能直接读取本地文件（甚至读出了仓库自己的 pyproject.toml），这句承诺是假的。已改成诚实描述，明确告诉模型"pandas 本身有这个能力，但不应该用它访问 sql_executor 已提供数据之外的内容"——这是文字层面的弱引导，不是代码层面的强制拒绝，模型完全可能不遵守
- **P0-1（已实测确认，未修复，判定为当前架构无法根治）**：pandas 模块是在沙箱外部用普通 `import pandas as pd` 加载的，它自己的模块全局命名空间里保留着真实、未受限的 `__builtins__`。实测 `pd.__builtins__['__import__']` 能绕过我们写的 `restricted_import` 包装，直接拿到真实的 `__import__`，进而访问任意系统模块
- **P0-2（已实测确认，未修复，判定为当前架构无法根治）**：任何一个被放行的内置类型（`str`/`list`/`int`）都自带完整的对象继承体系，`().__class__.__bases__[0].__subclasses__()` 能枚举到进程里所有已加载的类（实测枚举到 858 个，包含 `subprocess.Popen`），完全不经过 `import` 语句，我们做的所有 `__import__` 白名单限制对这条路径无效。这是 Python 沙箱逃逸里最经典的手法之一
- **关键结论**：P0-1 和 P0-2 不是"漏了一条规则没写"，是"用 Python 应用层的 `__builtins__`/白名单去限制 `exec()` 执行的代码"这个方案本身的天花板——这是 Python 语言图灵完备的性质决定的，继续在这个方向加更多规则只是在打地鼠。要真正解决，唯一可靠的办法是换成操作系统级别的隔离（独立进程 + 资源限制、容器、专门的沙箱运行时），这是明确的、留给生产化阶段的路线图条目，不是当前阶段能用打补丁方式修好的
- **推测性风险（Claude Code 已标注置信度，未实测触发）**：`cur.fetchall()` 一次性加载全部结果，无 LIMIT 的笛卡尔积查询理论上可能触发 `MemoryError`，不是 `sqlite3.Error` 的子类，会绕过现有 except 直接向上抛；`while True: pass` 这类死循环会让 `asyncio.to_thread` 的 worker 线程永久挂起（不是异常，是资源耗尽，需要区分对待）；重复触发可能逐步占满默认线程池，影响同进程内其他并发工具调用

### 异常传播机制 —— 已确认不是问题（无需修复）
- Claude Data Code 用真实脚本验证：`asyncio.to_thread` 包裹的函数抛出的异常，能正确传播到 `await` 处的 `try/except`，不存在"线程内异常被静默吞掉"的问题
- `sqlite3.OperationalError`（覆盖数据库被占用、磁盘 I/O 错误等）确认是 `sqlite3.Error` 的子类，现有 `except sqlite3.Error` 能正确捕获
- `python_executor.py` 的 `except Exception` 范围更宽，能捕获线程内几乎所有同步抛出的异常

### 端到端测试的可观测性限制
- **问题**：`get_state()` 顶层状态只保留两条消息（提问+报告），子图内部细节不可见；流式接口 `stream_subgraphs` 默认为 False；即使打开也只能看到节点跳转结构，具体 `tool_calls` 字段拿不到
- **根本原因**：Researcher 子图是在 `supervisor_tools` 函数内部用 `.ainvoke()` 程序化调用的，不是正式挂接的图节点，这层细节默认不会被转发
- **当前应对方式**：退回到间接证据交叉验证（最终数字精确匹配 + compress_research 过程描述 + Sources 引用列表点名工具/库名）
- **未验证**：是否能通过自定义 instrumentation（比如在 `.ainvoke()` 调用处手动加日志/回调）实现结构化断言，这条路径还没有尝试

### RAG 检索实验记录：删除文档开头 chunk 对排序的影响（2026-09-19）
- **实验目标**：验证删除 Markdown 文档第一个 `##` 标题前的"开头" chunk，是否能改善检索排序
- **实验结果**：前两个查询的 Top-3 排名基本不变，distance 数值也完全相同；例如第二个查询中 doc03 的 distance 仍为 0.448
- **实验结果（续）**：第三个查询出现轻微排序变化，但没有明显改善
- **结论**：当前实验不支持"开头 chunk 是主要检索问题来源"这一假设。不能据此断言该因素对所有查询都没有影响
- **下一步**：固定文档、切分策略、查询和 Top-k，只更换 Embedding 模型，进行对照测试

## 待办（尚未开始）

- [ ] mem0 跨会话记忆接入图（已验证 add/search 可用，未接入 deep_researcher 图）
- [ ] RAG（企业内部非结构化文档检索——注意这是与 sql_executor "故意不做 RAG" 完全不同的应用场景，前者是文档语义检索，后者是结构化 schema 探索，讲解时需要分清楚，不要说成矛盾）
- [ ] MCP Server 封装（把已稳定的 SQL/RAG 能力包装成 MCP 工具）
- [ ] 重复公司名测试（需要先往 demo.db 插入测试数据）
- [ ] 系统化可靠性测试集的 python_executor 部分（目前只有 sql_executor 有 3 条可靠性测试）


## 架构决策：三种能力的角色划分逻辑（2026-09-18）

**划分依据**：不按"内部 vs 外部数据来源"分角色，按"找资料 vs 算数字"这条业务分界线分：
- **找资料**（语义检索，没有精确数字答案）：网页搜索（已有 tavily_search）+ 内部文档 RAG（待实现）—— 归属 GeneralResearcher
- **算数字**（结构化查询+计算，有精确答案）：SQL 查询 + Python 统计计算（已有 sql_executor/python_executor）—— 归属 DataAnalyst

**理由**：真实企业场景里，一次研究任务（比如投研/尽调）经常连续问"外部行业情况"→"我们自己的数据库数字"→"我们内部之前的分析文档"，这三类问题是同一个人在同一个任务里连续会问的，不该因为"数据在内部还是外部"这个技术维度被拆成不相关的角色；但"找资料"和"算数字"在解决方式上（语义检索 vs 查询计算）是真正不同的技术路径，值得分开。

**结论**：RAG 归 GeneralResearcher，不影响 DataAnalyst 的职责边界，两件事可以独立排期，RAG 不依赖 DataAnalyst 节点分离先完成。

**排期**：RAG 接入 → RAG 评测验证 → DataAnalyst 代码层面节点分离（见上一条路线图）→ 全链路整合。完成后项目故事线：'找资料（网页+内部文档）+ 算数字（SQL+Python）+ 任务编排（Supervisor 拆分协调）' 三层能力清晰对应真实企业研究场景。


### RAG 检索实验记录：距离度量方式（l2 改 cosine）对排序的影响（2026-09-19）
- 实验目标：验证把 Chroma 默认的 l2 距离度量改成 cosine，是否能改善检索排序
- 改动方式：create_collection 时传入 configuration={"hnsw": {"space": "cosine"}}，用 Claude Code 实测确认此写法在 chromadb 1.5.9 下有效生效（读取 collection.configuration_json 确认为 cosine），并且是官方推荐的新写法（旧写法是 metadata={"hnsw:space": "cosine"}，两者等效但新写法更规范）
- 实验结果：distance 数值确实发生了变化（例如 doc03 那条从 0.448 变为 0.224，符合归一化向量下 l2 距离与 cosine 距离的数学换算关系），但四个查询的 Top-3 排序名次完全没有变化
- 结论：距离度量方式不是导致检索排序不理想的原因。这是第二个被排除的假设（第一个是"开头chunk信息稀薄"）
- 下一步：两个假设都被排除后，怀疑集中到默认 Embedding 模型（all-MiniLM-L6-v2，主要针对英文训练）本身对中文语义理解能力不足这一点，下一步测试更换为中文友好的 Embedding 模型（本地模型 BAAI/bge-small-zh，或 OpenAI text-embedding-3-small）


### RAG 检索实验记录：更换 Embedding 模型（2026-09-19，问题确认解决）
- 实验目标：在前两次实验排除"开头chunk"和"距离度量方式"之后，验证默认 Embedding 模型（all-MiniLM-L6-v2，主要面向英文训练）是否是导致中文检索排序不理想的真正原因
- 改动方式：只改 Embedding 模型，其他所有配置（chunk切分逻辑、cosine距离度量、查询文本、Top-k）保持不变，换成 OpenAI 的 text-embedding-3-small
- 实验结果：doc01_synthmotion_intro 从 Top-2 提升到 Top-1（SynthMotion业务问题），doc03_roboworks_intro 从 Top-2 提升到 Top-1（RoboWorks技术路线问题），两道核心测试题排序问题彻底解决
- 附带观察：无答案问题（Q12，查询不存在的公司）的检索结果 distance 明显变高（从约0.5升到0.6+），说明新模型对"不相关"的判断也更准确，这对后续判断"该不该拒绝回答"这类边界场景有帮助
- 结论：三次对照实验（开头chunk → 排除，距离度量 → 排除，Embedding模型 → 确认是根因），完整定位了中文语义检索效果不佳的真正原因。这个排查过程本身（控制变量、逐一排除、最后精确定位）比"一次性换好几个东西然后说效果变好"更有说服力
- 代价：从完全本地免费的 ONNX 模型，换成了每次建库/查询都要调用 OpenAI API 的方案，产生少量费用；如果追求完全本地化，下一步可以补测本地中文模型（BAAI/bge-small-zh）作为对比，但当前方案已经解决了核心问题，不是必须项


### RAG 检索实验记录：更换 Embedding 模型（2026-09-19）
- 实验目标：在排除"开头chunk"和"距离度量方式"这两个因素之后（注：这两轮实验样本量小，每轮仅4道测试题，只能说明"这两个因素在本次观察到的改善中不是主要来源"，不构成严格意义上的因果排除），测试更换 Embedding 模型是否能改善检索排序
- 改动方式：只改 Embedding 模型，其他配置（chunk切分、cosine距离度量、查询文本、Top-k）保持不变，从 chromadb 默认的 all-MiniLM-L6-v2 换成 OpenAI 的 text-embedding-3-small
- 实验结果：更换为 OpenAI text-embedding-3-small 后，两道核心中文检索问题（doc01/SynthMotion、doc03/RoboWorks）的正确文档由 Top-2 提升至 Top-1。该结果支持"当前测试集上更换 Embedding 模型能够改善检索排序"，但尚不足以证明它是所有检索问题的唯一根因
- 附带观察（不作为结论）：无答案问题（Q12）的 distance 数值在换模型前后发生变化，但不同 Embedding 模型输出的向量空间不同，distance 数值不能跨模型直接比较，这个变化不能被解读为"模型对不相关内容的判断更准确"或"置信度提升"，需要专门设计有答案/无答案对照测试集才能验证拒答能力
- 代价：从完全本地免费的方案，换成了每次建库/查询都调用 OpenAI API 的方案
- 尚未验证：本次只测了4道题，样本量不足以得出全面结论；后续需要用 ANSWER_KEY.md 里全部12道题做完整评测，尤其是跨文档、分歧识别、无答案这几类更复杂的场景


### 自然语言提问未明确指定内部知识库时，可能未触发内部检索
- 测试现象：针对 SynthMotion，未明确说明"内部知识库"的提问曾得到与内部模拟资料不符的泛化动画技术答案；明确指定内部知识库后，报告检索并整合了多份内部文档，并包含融资、产品、会议纪要等信息
- 初步判断：内部检索能力可能依赖用户在提问中显式提示"内部"等词语，存在检索路由和可用性风险
- 影响：用户可能不知道需要指定内部资料，系统可能生成表面流畅、实际与内部知识库不符的答案
- 后续改进方向：考虑让系统在相关问题下主动检索内部知识库，或在内部检索无结果时明确说明并区分外部信息；通过相同问题、相同配置的对照测试验证
- 证据边界：目前这是基于本次 E2E 结果的观察，尚不能仅凭最终报告证明措辞是唯一变量，也不能据此断言所有类似问题都会失败


## Q12 拒答测试：首次真实运行 + 人工复核记录（2026-09-20）

**运行状态**：主测试、对照测试均 run_status=success，首次真实运行（此前多轮讨论中的"运行结果"均为误判，未曾真正执行，已在本轮修正）。

**主测试结论**：拒答信号命中（final_report 明确说明未找到 XYZ 机器人公司记录，方向正确）。cross_company_misattribution_signal 命中，人工核实 matched_phrase="2026年9月"、matched_company="SynthMotion"、overlap_length=7——确认是日期字面巧合（报告生成时间戳与 doc10 会议日期标注重合），不是真正的技术特征张冠李戴，本次判定为假阳性，但已定位到具体证据支撑，不是主观猜测。

**检测规则缺陷（新发现）**：MISATTRIBUTION_MIN_OVERLAP_CHARS=6 这个阈值对日期类高频、低信息熵字符串区分度不足，容易被日期巧合触发假阳性。待办：后续可考虑对日期格式字符串单独做排除处理，或提高纯数字+年月日格式片段的判定阈值。

**主测试附带问题**：final_report 声称检索了"所有企业数据库表、邮件、备忘录"等，但系统实际检索能力仅为 Chroma 语义文档检索，不涉及数据库表或邮件——这是检索范围的夸大表述，不影响拒答结论本身的正确性，但影响回答的诚实度，应作为独立问题记录。

**对照测试结论**：核心事实（融资金额8000万美元Series C、供应链态度从8月谨慎转9月乐观、技术领先性评价）经过原文比对，均有 doc01/doc08/doc10 支持。但"多家龙头企业试点并取得积极反馈"、"量产节奏较快...优于市场普遍预期"这类具体表述，在提供的四篇文档原文中找不到对应依据，判定为模型自主扩写，不是纯粹转述——这是比"完全编造"更隐蔽的一类事实忠实度问题，因为整体回答大部分内容可信，容易让扩写部分被忽略。

**方法论教训**：本轮复核过程中，曾两次在未看到真实原始数据（真实JSON字段值、原始文档全文）的情况下给出确定性结论（一次误判"运行已成功"，一次误判"张冠李戴信号是假阳性"但依据不足），均被要求补充原始证据后才能定论。这印证了 Q6/Q12 整个设计的核心原则——"证据强度必须匹配结论强度"，这条原则同样适用于人工复核环节本身，不只适用于被测系统。

**human_review_status**：本次以上述文字记录代替单一 pass/fail 判定，符合当初"不自动判定通过失败，必须人工复核并留痕"的设计要求。


## Memory MVP：四项针对性核查结论（2026-09-21）

**核查1：跨thread验证的证据边界**
- 已确认（直接证据）：新thread的search_memory调用，真实检索到了旧thread写入的记忆（[MEMORY DEBUG]日志实证）
- 未直接验证（只有间接证据）：检索到的记忆是否真的被模型采纳、体现在最终research_brief里——目前只有一次观察性证据（某次运行的research_topic里出现了未在当次提问中提及的年份范围限定，与此前存入的记忆内容吻合），不是通过追踪prompt_content变量得到的直接证据

**核查2：保存与检索的生命周期**
- 已确认：save_memory保存的是user_messages_text（用户原始输入），不是模型生成的research_brief或最终报告——"研究未完成时保存不准确摘要"这个风险，因设计上从不保存摘要，可判定为不存在
- search_memory的结果在生成research_brief之前被拼入prompt_content，此后不再使用（每轮独立检索）

**核查3：用户隔离与持久化**
- 已确认：DEMO_USER_ID是刻意的单用户演示简化，非真实多用户系统（代码注释已声明），多用户隔离机制本身已在test_memory_manager.py用不同user_id独立验证过
- 新增证据（意外发现，非专门设计的验证）：test_memory_manager.py本次重跑，检索到的记忆created_at时间戳为今天早些时候（2026-09-20T13:11:36），证明跨越了今天多次langgraph dev服务重启和独立脚本运行，本地Qdrant持久化存储依然保留数据、可被检索——这条证据是重跑已有测试时顺带获得的，不是专门为验证持久化设计的测试

**核查4：失败处理与单元测试**
- 已确认：save_memory/search_memory内部用try/except包裹，失败返回{"success": False, ...}而不抛异常，不会阻塞主研究流程（代码设计层面确认）
- test_memory_manager.py（4条用例：基本存取、更新覆盖、用户隔离、无记忆场景）本次重新运行，全部通过

**综合结论**：Memory MVP的核心链路（写入、跨线程检索、失败降级、用户隔离机制）有扎实的直接证据支持。唯一保留"未直接验证"标注的是"检索结果是否真正影响模型生成内容"这一环——现有证据是间接的（内容吻合），不是结构化追踪的直接证据，这与Q6/Q12里"raw_notes指纹匹配"的证据强度是同一类型，需要保持一致的诚实标注，不能因为已有大量直接证据支撑其他环节，就把这一环也默认为已验证。
