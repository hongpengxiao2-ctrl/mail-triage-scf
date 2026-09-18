"""
构建腾讯云函数部署包 ../qq-mail-triage-scf-deploy.zip
- 打包 handler.py 与依赖（requests、cos-python-sdk-v5）
- 用法: python build_scf.py [--force]

依赖安装用当前解释器的 pip（`sys.executable -m pip`），因此请在装有
requests / cos-python-sdk-v5 的虚拟环境里运行。也可用环境变量 PIP_CMD
指定任意 pip 可执行文件。

网络容错：pip 不可用（代理 502、断网等）时，会自动从**上一次成功构建的
部署包**里回收依赖，避免因一次网络抖动就构建不出来。
`--force` 可强制重新安装依赖。
"""
import os
import shutil
import subprocess
import sys
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
BUILD_DIR = os.path.join(BASE, "build")
OUT_ZIP = os.path.join(os.path.dirname(BASE), "qq-mail-triage-scf-deploy.zip")

# (pip 包名, 导入时的目录名)
DEPS = [("requests", "requests"), ("cos-python-sdk-v5", "qcloud_cos")]


def _pip_install(pkg):
    """优先用 `python -m pip`；PIP_CMD 被显式指定时直接用该可执行文件。"""
    if os.environ.get("PIP_CMD"):
        cmd = [os.environ["PIP_CMD"], "install", "--no-cache-dir", "-t", BUILD_DIR, pkg]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-t", BUILD_DIR, pkg]
    subprocess.run(cmd, check=True)


def _deps_ready():
    return all(os.path.isdir(os.path.join(BUILD_DIR, mod)) for _, mod in DEPS)


def _restore_deps_from_zip():
    """从上一次成功构建的部署包里回收依赖（离线兜底）。"""
    if not os.path.exists(OUT_ZIP):
        return False
    try:
        with zipfile.ZipFile(OUT_ZIP) as z:
            for name in z.namelist():
                if name == "handler.py":      # 只回收依赖，不回收旧业务代码
                    continue
                z.extract(name, BUILD_DIR)
        return _deps_ready()
    except Exception as e:
        print(f"    从旧部署包回收失败：{e}")
        return False


def install_deps():
    os.makedirs(BUILD_DIR, exist_ok=True)
    print("安装依赖...")
    for pkg, _ in DEPS:
        try:
            _pip_install(pkg)
        except subprocess.CalledProcessError:
            print(f"  ⚠️ {pkg} 安装失败（多为代理/网络问题），尝试从上一次构建回收…")
            if _restore_deps_from_zip():
                print("  ✅ 已从上次构建恢复依赖，继续打包")
                return
            raise


def main():
    force = "--force" in sys.argv
    if os.path.exists(BUILD_DIR) and not force:
        if _deps_ready():
            print("依赖已就绪，跳过安装（--force 可强制重装）")
        else:
            install_deps()
    else:
        if os.path.exists(BUILD_DIR):
            shutil.rmtree(BUILD_DIR)
        os.makedirs(BUILD_DIR, exist_ok=True)
        install_deps()

    # 清理 Windows 专用产物：.pyd 编译模块与入口脚本在 SCF(Linux) 上无效且占体积。
    # 相关库（charset_normalizer / crcmod）均有纯 Python 回退实现，删除是安全的。
    removed = []
    for root, dirs, files in os.walk(BUILD_DIR):
        # 顶层 bin/ 是 pip 生成的 Windows 启动器目录，整个删掉
        if root == BUILD_DIR and "bin" in dirs:
            shutil.rmtree(os.path.join(root, "bin"), ignore_errors=True)
            removed.append(os.path.join(root, "bin") + os.sep)
            dirs.remove("bin")
        for f in files:
            if f.endswith((".pyd", ".exe", ".dll")):
                p = os.path.join(root, f)
                removed.append(p)
                os.remove(p)
    print(f"清理 Windows 专用文件 {len(removed)} 项")

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
