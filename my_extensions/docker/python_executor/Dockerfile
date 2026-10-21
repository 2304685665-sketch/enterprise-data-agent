# python_executor 沙箱镜像。
#
# 设计目标：真正的安全边界来自容器层面的隔离（见下方 docker run 参数），
# 不是靠本镜像本身。本镜像只负责提供一个最小的、预装 pandas 的 Python 运行时。
#
# 必须配合以下 `docker run` 参数使用才能构成有效隔离（见 python_executor.py）：
#   --network none        容器内无法访问网络（宿主机网络栈完全不可达）
#   --read-only            容器根文件系统只读
#   --tmpfs /tmp           唯一可写目录是内存中的 tmpfs，容器销毁后不留痕
#   --memory / --cpus      限制内存和 CPU，防止资源耗尽型代码拖垮宿主机
#   --pull=never           禁止隐式从镜像仓库拉取，镜像必须已在本机构建好
#   --rm                   容器退出后立即删除，不留下可被检查/复用的状态
#   不挂载任何宿主机目录 (无 -v)  容器内看不到宿主机文件系统的任何内容
#
# 即使模型生成的代码试图 import os / subprocess 并执行任意系统调用，
# 影响范围也被严格限制在这个一次性、无网络、无宿主机文件可见性的容器内部，
# 不依赖任何 Python 应用层的 __builtins__/白名单限制
# （旧方案已被 IMPROVEMENTS.md 记录为可绕过，不再作为安全边界）。

FROM python:3.11-slim

RUN pip install --no-cache-dir "pandas>=2.3.1"

RUN useradd --create-home --uid 1000 sandbox
USER sandbox
WORKDIR /tmp

# 不设置 CMD/固定脚本路径：运行时通过 stdin 把代码喂给 python3，
# 配合 `docker run -i`，无需在容器内写文件、无需挂载卷。
ENTRYPOINT ["python3", "-u", "-"]
