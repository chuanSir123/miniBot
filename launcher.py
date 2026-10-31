import os
import importlib.util
from importlib import metadata
import subprocess
import sys
import time
from pathlib import Path

# 确保在 Windows 控制台下支持 UTF-8 编码与 Emoji 正常输出
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent
PYPI_INDEX = "https://pypi.org/simple"
TSINGHUA_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
ALIYUN_INDEX = "https://mirrors.aliyun.com/pypi/simple/"
PACKAGE_INDEXES = (TSINGHUA_INDEX, ALIYUN_INDEX, PYPI_INDEX)

# 模式所需依赖声明
DEPS_PET = ["PyQt5", "Pillow"]
DEPS_NAPCAT = ["websockets>=14,<16", "Pillow"]

# main.py 用这个退出码表示"请把我重新拉起来"（聊天里 restart_bot 触发的重启）。
RESTART_EXIT_CODE = 42
# 标记子进程"外面有人看着"，main.py 就不会自己再拉一个。
SUPERVISED_ENV = "MINIBOT_SUPERVISED"
# 连续多少次"秒退"就判定为启动崩溃，停止自动重启。
MAX_FAST_RESTARTS = 3
# 少于这个运行秒数算"秒退"。
FAST_RESTART_SEC = 30


def check_module_installed(mod_name: str) -> bool:
    try:
        if mod_name.startswith("websockets"):
            version = int(metadata.version("websockets").split(".")[0])
            return 14 <= version < 16
        if mod_name == "Pillow":
            return importlib.util.find_spec("PIL") is not None
        elif mod_name == "pywin32":
            return importlib.util.find_spec("win32gui") is not None
        return importlib.util.find_spec(mod_name) is not None
    except Exception:
        return False


def install_deps(dep_list):
    # Combined mode shares Pillow between both channels.
    missing = [d for d in dict.fromkeys(dep_list) if not check_module_installed(d)]
    if not missing:
        return True

    print(f"\n[按需依赖安装器] 检测到缺少组件: {missing}")
    for position, index in enumerate(PACKAGE_INDEXES):
        print(f"[按需依赖安装器] 正在使用 {index} 安装依赖 ...")
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--only-binary=:all:",
            "--timeout",
            "20",
            "--retries",
            "1",
            "--index-url",
            index,
        ] + missing
        try:
            ret = subprocess.call(cmd)
        except OSError as exc:
            print(f"[按需依赖安装器] 安装程序启动失败: {exc}")
            ret = 1
        if ret == 0:
            # Only change this project's venv, never the user's global pip settings.
            if Path(sys.prefix).resolve() == (PROJECT_ROOT / ".venv").resolve():
                subprocess.call(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "config",
                        "--site",
                        "set",
                        "global.index-url",
                        index,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            print("[按需依赖安装器] ✅ 组件安装成功！\n")
            return True
        if position + 1 < len(PACKAGE_INDEXES):
            print("[按需依赖安装器] 当前源安装失败，自动切换备用源重试。")

    print(
        f"[按需依赖安装器] ❌ 所有安装源均失败。当前 Python: {sys.version.split()[0]} ({sys.executable})"
    )
    print(
        "请检查网络/代理；若提示 No matching distribution，也可能是该 Python 版本或架构没有对应安装包。"
    )
    return False


def main():
    os.chdir(PROJECT_ROOT)
    from webui.server import run_server

    if sys.stdin.isatty():
        _show_default_skill_guide()
    while True:
        print("\n" + "=" * 60)
        print("          🤖 MiniBot 极简主动交互智能体启动器")
        print("=" * 60)
        print("请选择本次运行模式（按需轻量安装依赖，绝不多装）：")
        print("")
        print(" [1] 仅桌宠交互 (IKun 鸡哥桌宠)")
        print("     - 特点: 本地旁听流感知 + 悬停右下角输入 + 头顶气泡主动互动")
        print("     - 依赖: PyQt5, Pillow（按需安装）")
        print("")
        print(" [2] 仅 NapCat QQ 交互 (内嵌扫码 QQ 机器人)")
        print("     - 特点: 内嵌 NapCat 进程 + 终端输出二维码直接扫码登录 + 消息收发")
        print("     - 依赖: websockets, Pillow（不含 NapCat/QQ 进程）")
        print("")
        print(" [3] 全功能模式 (桌宠 + NapCat QQ)")
        print("     - 特点: 桌面与 QQ 双通道同时在线，共享同一个快脑与记忆")
        print("")
        print(
            " [5] 本地终端交互 (轻量，无桌宠)\n [4] ⚙️ 偏好设置中心 (浏览器打开 http://127.0.0.1:18080)"
        )
        print(
            "     - 特点: 填写 Jev API Key(前往https://decision.vllm-sr.ai/)、VLM Key 并测试连接"
        )
        print("")
        print(" [0] 退出")
        print("=" * 60)

        print(" [6] 默认技能安装引导（AnySearch / ego-lite）")
        choice = input("请输入选项 [1/2/3/4/5/6/0]: ").strip()

        if choice == "1":
            if install_deps(DEPS_PET):
                _check_first_time_config()
                return _run_main("pet")
            return 1

        elif choice == "2":
            if install_deps(DEPS_NAPCAT):
                _check_first_time_config()
                return _run_main("napcat")
            return 1

        elif choice == "3":
            if install_deps(DEPS_PET + DEPS_NAPCAT):
                _check_first_time_config()
                return _run_main("all")
            return 1

        elif choice == "4":
            print("\n[WebUI] 正在打开浏览器配置中心...")
            run_server(port=18080, open_browser=True, blocking=True)

        elif choice == "5":
            if install_deps(["Pillow"]):
                _check_first_time_config()
                return _run_main("local")
            return 1

        elif choice == "6":
            _show_default_skill_guide(force=True)
            run_server(port=18080, open_browser=True, blocking=True)

        elif choice == "0":
            print("再见！")
            sys.exit(0)
        else:
            print("⚠️ 无效输入，请重新选择。")


def _show_default_skill_guide(force=False):
    import json
    from core.config import load_config
    from core.ego import status

    marker = PROJECT_ROOT / "data" / "onboarding.json"
    try:
        seen = json.loads(marker.read_text(encoding="utf-8")).get("default_skills_v1")
    except (OSError, ValueError, AttributeError):
        seen = False
    if seen and not force:
        return
    print("\n[首次启动 / 默认技能安装引导]")
    print("  模型：Jev Key 获取 https://decision.vllm-sr.ai/；VLM 使用你的兼容接口。")
    cfg = load_config()
    configured = bool(
        cfg["anysearch"]["api_key"] or os.environ.get("ANYSEARCH_API_KEY")
    )
    print(
        "  AnySearch："
        + (
            "已配置 Key。"
            if configured
            else "未填 Key，可匿名搜索（低配额）。在设置页用真实邮箱注册，或访问 https://anysearch.com/console/api-keys 。"
        )
    )
    try:
        browser = status()
        if not browser["environment"]["supported"]:
            print("  ego-lite：" + browser["environment"]["reason"])
        elif browser["installed_cli"]:
            print("  ego-lite：已找到 CLI，第一次浏览器任务会读取指南并验证连接。")
        else:
            print(
                "  ego-lite：未找到 CLI。选 [6] 打开设置，点击按本机环境安装；完成安装后请在应用中完成首次引导。"
            )
    except (OSError, ValueError) as exc:
        print("  ego-lite 环境检查：" + str(exc))
    print("  选 [6] 可随时重新打开安装引导；首次使用浏览器也会按需引导安装。")
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        temp = marker.with_suffix(".tmp")
        temp.write_text(json.dumps({"default_skills_v1": True}), encoding="utf-8")
        temp.replace(marker)
    except OSError:
        pass  # A read-only workspace must still be able to launch.


def _check_first_time_config():
    from core.config import is_configured
    from webui.server import run_server

    if not is_configured():
        print("\n" + "!" * 60)
        print(" [提示] 尚未配置 Jev (https://decision.vllm-sr.ai/) 或 VLM API Key！")
        print(" [建议] 现在自动打开浏览器配置中心进行填写与连通性测试。")
        print("!" * 60)
        ans = input("是否立即打开配置页面？[Y/n]: ").strip().lower()
        if ans != "n":
            run_server(port=18080, open_browser=True, blocking=True)


def _run_main(mode: str):
    """跑 main.py，并充当重启看护：main 以 RESTART_EXIT_CODE 退出就重新拉起。"""
    cmd = [sys.executable, str(PROJECT_ROOT / "main.py"), "--mode", mode]
    env = dict(os.environ)
    env[SUPERVISED_ENV] = "1"
    fast_restarts = 0
    while True:
        started = time.time()
        try:
            code = subprocess.call(cmd, cwd=PROJECT_ROOT, env=env)
        except KeyboardInterrupt:
            return 130
        if code != RESTART_EXIT_CODE:
            return code
        runtime = time.time() - started
        fast_restarts = fast_restarts + 1 if runtime < FAST_RESTART_SEC else 0
        if fast_restarts > MAX_FAST_RESTARTS:
            print(
                "[启动器] 连续多次快速重启，已停止自动重启。"
                "请检查上面的日志确认启动失败原因。"
            )
            return code
        print("[启动器] ↻ MiniBot 请求重启，正在重新拉起…")
        time.sleep(2)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
