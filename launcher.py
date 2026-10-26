import importlib.util
import os
import subprocess
import sys
from pathlib import Path

# 确保在 Windows 控制台下支持 UTF-8 编码与 Emoji 正常输出
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent
TSINGHUA_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"

# 模式所需依赖声明
DEPS_PET = ["PyQt5", "Pillow", "pywin32"]
DEPS_NAPCAT = ["websockets"]


def check_module_installed(mod_name: str) -> bool:
    try:
        if mod_name == "Pillow":
            return importlib.util.find_spec("PIL") is not None
        elif mod_name == "pywin32":
            return importlib.util.find_spec("win32gui") is not None
        return importlib.util.find_spec(mod_name) is not None
    except Exception:
        return False


def install_deps(dep_list):
    missing = [d for d in dep_list if not check_module_installed(d)]
    if not missing:
        return True

    print(f"\n[按需依赖安装器] 检测到缺少组件: {missing}")
    print(f"[按需依赖安装器] 正在通过清华大学开源镜像源高速下载安装: {missing} ...")

    cmd = [
        sys.executable, "-m", "pip", "install",
        "-i", TSINGHUA_INDEX,
        "--trusted-host", "pypi.tuna.tsinghua.edu.cn"
    ] + missing

    try:
        ret = subprocess.call(cmd)
        if ret == 0:
            print("[按需依赖安装器] ✅ 组件安装成功！\n")
            return True
        else:
            print(f"[按需依赖安装器] ❌ 安装失败 (退出码: {ret})")
            return False
    except Exception as e:
        print(f"[按需依赖安装器] 安装异常: {e}")
        return False


def main():
    os.chdir(PROJECT_ROOT)
    from core.config import is_configured
    from webui.server import run_server

    while True:
        print("\n" + "=" * 60)
        print("          🤖 MiniBot 极简主动交互智能体启动器")
        print("=" * 60)
        print("请选择本次运行模式（按需轻量安装依赖，绝不多装）：")
        print("")
        print(" [1] 仅桌宠交互 (IKun 鸡哥桌宠)")
        print("     - 特点: 本地旁听流感知 + 悬停右下角输入 + 头顶气泡主动互动")
        print("     - 依赖: PyQt5, Pillow, pywin32 (~40MB)")
        print("")
        print(" [2] 仅 NapCat QQ 交互 (内嵌扫码 QQ 机器人)")
        print("     - 特点: 内嵌 NapCat 进程 + 终端输出二维码直接扫码登录 + 消息收发")
        print("     - 依赖: websockets (<5MB)")
        print("")
        print(" [3] 全功能模式 (桌宠 + NapCat QQ)")
        print("     - 特点: 桌面与 QQ 双通道同时在线，共享同一个快脑与记忆")
        print("")
        print(" [4] ⚙️ 偏好设置中心 (浏览器打开 http://127.0.0.1:18080)")
        print("     - 特点: 填写 Jev API Key(前往bamboonode.dev)、VLM Key 并测试连接")
        print("")
        print(" [0] 退出")
        print("=" * 60)

        choice = input("请输入选项 [1/2/3/4/0]: ").strip()

        if choice == "1":
            if install_deps(DEPS_PET):
                _check_first_time_config()
                _run_main("pet")
            break

        elif choice == "2":
            if install_deps(DEPS_NAPCAT):
                _check_first_time_config()
                _run_main("napcat")
            break

        elif choice == "3":
            if install_deps(DEPS_PET + DEPS_NAPCAT):
                _check_first_time_config()
                _run_main("all")
            break

        elif choice == "4":
            print("\n[WebUI] 正在打开浏览器配置中心...")
            run_server(port=18080, open_browser=True, blocking=True)

        elif choice == "0":
            print("再见！")
            sys.exit(0)
        else:
            print("⚠️ 无效输入，请重新选择。")


def _check_first_time_config():
    from core.config import is_configured
    from webui.server import run_server

    if not is_configured():
        print("\n" + "!" * 60)
        print(" [提示] 尚未配置 Jev (bamboonode.dev) 或 VLM API Key！")
        print(" [建议] 现在自动打开浏览器配置中心进行填写与连通性测试。")
        print("!" * 60)
        ans = input("是否立即打开配置页面？[Y/n]: ").strip().lower()
        if ans != "n":
            run_server(port=18080, open_browser=True, blocking=True)


def _run_main(mode: str):
    cmd = [sys.executable, "main.py", "--mode", mode]
    try:
        subprocess.call(cmd)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
