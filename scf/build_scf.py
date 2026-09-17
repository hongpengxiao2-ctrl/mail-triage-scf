"""
构建腾讯云函数部署包 ../qq-mail-triage-scf-deploy.zip
- 打包 handler.py 与依赖（requests、cos-python-sdk-v5）
- 用法: python build_scf.py

依赖安装用当前解释器的 pip（`sys.executable -m pip`），
因此请在装有 requests / cos-python-sdk-v5 的虚拟环境里运行本脚本；
也可用环境变量 PIP_CMD 指定任意 pip 可执行文件。
"""
import os
import shutil
import subprocess
import sys
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
BUILD_DIR = os.path.join(BASE, "build")
OUT_ZIP = os.path.join(os.path.dirname(BASE), "qq-mail-triage-scf-deploy.zip")

PIP = os.environ.get("PIP_CMD", "") or sys.executable


def _pip_install(dep):
    """优先用 `python -m pip`，PIP_CMD 被显式指定时直接用该可执行文件。"""
    if os.environ.get("PIP_CMD"):
        cmd = [PIP, "install", "--no-cache-dir", "-t", BUILD_DIR, dep]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-t", BUILD_DIR, dep]
    subprocess.run(cmd, check=True)


# imaplib / email / json / re 均为标准库；仅 requests 与 COS SDK 需要打包
DEPS = ["requests", "cos-python-sdk-v5"]


def main():
    if os.path.exists(BUILD_DIR):
        shutil.rmtree(BUILD_DIR)
    os.makedirs(BUILD_DIR, exist_ok=True)

    print("安装依赖...")
    for dep in DEPS:
        _pip_install(dep)

    # 清理 Windows 专用产物：.pyd 编译模块与可执行脚本在 SCF(Linux) 上无效且占体积。
    # 相关库（charset_normalizer / crcmod）均有纯 Python 回退实现，删除是安全的。
    removed = []
    for root, dirs, files in os.walk(BUILD_DIR):
        if os.path.basename(root) == "bin":
            removed.append(os.path.join(root, ""))
            dirs[:] = []
            continue
        for f in files:
            if f.endswith((".pyd", ".exe", ".dll")):
                p = os.path.join(root, f)
                removed.append(p)
                os.remove(p)
    print(f"清理 Windows 专用文件 {len(removed)} 个")

    shutil.copy(os.path.join(BASE, "handler.py"), os.path.join(BUILD_DIR, "handler.py"))

    print("打包...")
    if os.path.exists(OUT_ZIP):
        os.remove(OUT_ZIP)
    with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(BUILD_DIR):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                if f.endswith(".pyc"):
                    continue
                full = os.path.join(root, f)
                z.write(full, os.path.relpath(full, BUILD_DIR))

    size_mb = os.path.getsize(OUT_ZIP) / 1024 / 1024
    print(f"完成: {OUT_ZIP} ({size_mb:.2f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
