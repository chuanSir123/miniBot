"""Deterministic completion gates. Tool success alone is not test evidence.

分工：这里只核对"客观发生过什么"——语法能不能编译、命令有没有真的跑出
断言结算行、页面有没有在浏览器里渲染且无运行错误。语义判断（回复是否符合
目标、画面是否满足要求）不在这里做，那是 Jev 的唯一职责。

历史教训：曾用 `re.match(r'^(pytest|npm test|...)')` 判断"这条命令是不是
测试"。那是在猜意图：`python tests/smoke.py`、`uv run pytest`、
`cd web && npx vitest`、`.\run_tests.ps1` 全被判成"没测试"，模型只好重跑；
而 `pytest && echo ok` 又能骗过。现在改为**看输出里的断言结算行**，
只排除根本不可能执行断言的命令（回显、列目录、语法编译、查版本）。
"""

import os
import re
from pathlib import Path


CODE_SUFFIXES = {'.py', '.js', '.mjs', '.cjs', '.jsx', '.ts', '.tsx',
                 '.sh', '.bat', '.ps1', '.go', '.rs', '.java', '.cpp', '.c', '.cs', '.vue', '.svelte'}
WEB_SUFFIXES = {'.html', '.htm', '.svg', '.css', '.jsx', '.tsx', '.vue', '.svelte'}

# 不可能执行断言、因而不算测试证据的命令。白名单换成黑名单：真正的测试调用
# 方式太多（自定义脚本、uv/pdm/poetry、monorepo 的 cd 前缀），列不全。
NOT_A_TEST = re.compile(
    r'^\s*(?:&\s*)?(?:'
    r'echo|write-output|write-host|printf|'
    r'get-childitem|dir\b|ls\b|cat\b|type\b|where\b|which\b|'
    r'[\w:./\\-]*python(?:3(?:\.\d+)?(?:\.exe)?|\.exe)?\s+-m\s+(?:py_compile|compileall)\b|'
    r'[\w:./\\-]*python(?:3(?:\.\d+)?(?:\.exe)?|\.exe)?\s+(?:--version|-V)\b|'
    r'node\s+--version\b|npm\s+(?:--version|-v)\b|npx\s+tsc\b|tsc\b'
    r')', re.I)

# 断言结算行：pytest/unittest/jest/vitest/node:test/go/cargo/dotnet/mocha/rspec，
# 以及自定义脚本常见的 "All 5 tests passed" / "5 项测试通过"。
TEST_REPORT = re.compile(
    r'(?:\bRan\s+[1-9]\d*\s+tests?\b'
    r'|\b[1-9]\d*\s+(?:passed|passing)\b'
    r'|\b[1-9]\d*\s+tests?\s+passed\b'
    r'|\ball\s+\d+\s+tests?\s+passed\b'
    r'|#\s*pass\s+[1-9]\d*\b'
    r'|\bPassed!\s*-\s*\d+'
    r'|\bTests?:\s*.*\b[1-9]\d*\s+passed\b'
    r'|test result:\s*ok\b'
    r'|^ok\s+\S+'
    r'|\b[1-9]\d*\s+(?:项|个)?(?:测试|用例|断言)(?:全部)?通过)',
    re.I | re.M)

# 失败标记。退出码非 0 已经单独拦，这里补的是"退出码 0 但输出里报了失败"
# 和"跑了 0 个用例"这两类假通过。
TEST_FAILURE = re.compile(
    r'\b[1-9]\d*\s+failed\b|\b[1-9]\d*\s+failures?\b|\bFAIL(?:ED|URES?)\b'
    r'|#\s*fail\s+[1-9]\d*|^not ok\b'
    r'|\bRan\s+0\s+tests\b|\bno tests (?:ran|found)\b'
    r'|\b0\s+tests?\s+(?:ran|found|executed)\b',
    re.I | re.M)


# 页面里存在这些结构，说明"这一帧"不足以证明效果。判断依据是文件内容，不是
# 模型的自我声明——所以它仍然属于"查记录"，不需要问 Jev。
# 刻意不收 transition：hover 变色这类一次性过渡很常见，按它要求多帧会逼模型
# 为了过门禁去伪造帧数。
MOTION_SIGNALS = re.compile(
    r'@keyframes'
    r'|\banimation(?:-name|-duration|-iteration-count)?\s*:'
    r'|\brequestAnimationFrame\s*\('
    r'|\bsetInterval\s*\('
    r'|<canvas\b|<video\b|<animate\b|<animateMotion\b|<animateTransform\b',
    re.I)

MOTION_MIN_FRAMES = 3


def motion_expected(path, scope):
    """这个页面里有没有"持续动起来"的东西。

    只读改动过的产物本身：CSS 动画、动画循环、canvas/video/SVG 动画。读不到
    文件时返回 False——门禁不能因为读不到就假定有动画。
    """
    from core.workspace import resolve_file
    try:
        text = resolve_file(path, scope).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return False
    return bool(MOTION_SIGNALS.search(text[:400000]))


def source_snapshot(root):
    """Track workspace source changes made by commands/skills as well as edits."""
    snapshot = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in {'.git', 'node_modules', '.venv', 'venv', '__pycache__', 'target', 'dist'} and not Path(directory, d).is_symlink()]
        for name in files:
            path = Path(directory, name)
            if path.suffix.lower() in CODE_SUFFIXES | WEB_SUFFIXES:
                stat = path.stat()
                snapshot[str(path.resolve())] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def last_mutation_index(trace):
    """最后一次真正写入的位置；之前的测试与截图都已失效。

    gate、程序核验摘要、harness 的截图检查必须用同一个定义，否则会出现
    "门禁认为截图过期、摘要认为有效"这种自相矛盾。
    """
    return max(
        (i for i, entry in enumerate(trace or ())
         if entry.get('result', {}).get('changed_files')
         or (entry.get('tool') in {'write', 'edit'}
             and entry.get('result', {}).get('ok'))),
        default=-1)


def test_evidence(trace, after=-1):
    """last_mutation 之后真正跑出断言结算行的命令 → {command: result}。

    同一条命令重跑多次时后一次覆盖前一次（修完再跑是常态）。输出里只有
    "failure" 标记的命令也计入，这样门禁能报"测试失败"而不是"没测试"。
    """
    found = {}
    for index, entry in enumerate(trace or ()):
        if index <= after or entry.get('tool') != 'bash':
            continue
        command = str(entry.get('arguments', {}).get('command', '')).strip()
        if not command or NOT_A_TEST.match(command):
            continue
        output = str(entry.get('result', {}).get('output', ''))
        if TEST_REPORT.search(output) or TEST_FAILURE.search(output):
            found[command] = entry.get('result', {})
    return found


def completion_gate(trace, modified_files, scope, motion_required=True):
    """Return actionable feedback, or empty string when hard gates are satisfied.

    modified_files 必须是**本轮**真正改动过的文件；续作时带上的历史改动不算，
    否则用户发一句"继续"就会被要求重新测试和截图。

    motion_required 为 True 时，含动画结构的页面必须交出多帧截图且帧间确有
    变化——"要几帧才够"是程序按文件内容定的，不问模型，也不让模型自报。
    """
    from core.workspace import resolve_file

    last_edit = last_mutation_index(trace)
    fresh = trace[last_edit + 1:]
    code = [p for p in modified_files if Path(p).suffix.lower() in CODE_SUFFIXES]
    web = [p for p in modified_files if Path(p).suffix.lower() in WEB_SUFFIXES]
    for path in code:
        if Path(path).suffix.lower() == '.py':
            try:
                actual = resolve_file(path, scope)
                compile(actual.read_text(encoding='utf-8-sig'), str(actual), 'exec')
            except (OSError, SyntaxError, UnicodeError) as exc:
                return f'【代码语法核验失败】{path}: {exc}。请修复文件，再运行功能测试。'

    if code:
        tests = test_evidence(trace, after=last_edit)
        if not tests:
            broken = next((str(t.get('arguments', {}).get('command', '')) for t in reversed(fresh)
                           if t.get('tool') == 'bash'
                           and not t.get('result', {}).get('ok')), '')
            hint = f'最后一次修改后 {broken} 执行失败，先修复再补测试。' if broken else ''
            return ('【功能测试缺失】最后一次修改后没有跑出任何断言结果。请用 bash 运行'
                    '相关功能测试（自定义测试脚本、pytest/unittest、node --test、npm test 等都可以），'
                    '输出里要能看到通过/失败的用例数量。列目录、截图、语法编译和启动程序都不算。' + hint)
        for command, latest in tests.items():
            output = str(latest.get('output', ''))
            if (not latest.get('ok') or latest.get('exit_code') not in (0, None)
                    or latest.get('timeout') or latest.get('cancelled')
                    or TEST_FAILURE.search(output)):
                return f'【功能测试失败】{command}：请根据实际错误修改代码并重跑，不得宣称通过：{output[-2000:]}'
            if not TEST_REPORT.search(output):
                return '【测试证据不足】退出码为0，但输出里没有执行过的用例数量；请运行带断言且报告测试结果的测试，不能用空测试或输出“成功”代替。'

    for path in web:
        expected = resolve_file(path, scope).resolve()
        captures = []
        for entry in fresh:
            result = entry.get('result', {}) or {}
            if entry.get('tool') != 'capture_screen':
                continue
            target = result.get('target', '')
            if target.startswith(('http://', 'https://')):
                # 开发服务器 URL 无法映射回文件，接受；画面是否达标由放行判定看图决定。
                captures.append(result)
            elif target and not target.startswith('file://'):
                actual = resolve_file(target, scope).resolve()
                if actual == expected or (expected.suffix.lower() not in {'.html', '.htm', '.svg'} and actual.suffix.lower() in {'.html', '.htm'}):
                    captures.append(result)
        if not captures:
            return f'【页面核验缺失】最后一次修改后尚未渲染目标 {path}；请 capture_screen 指定该文件或实际开发服务器URL，桌面截图和旧截图不算证据。'
        captures = captures[-1:]
        if not captures[0].get('ok'):
            return f'【页面核验失败】{path} 最新渲染失败，请修复后重新 capture_screen：{captures[0].get("runtime_errors") or captures[0].get("error", "截图失败")}'
        if not any(c.get('runtime_checked') is True and not c.get('runtime_errors') for c in captures):
            return f'【浏览器运行核验缺失】{path} 缺少无运行错误的CDP检测结果，请排查浏览器错误或安装websockets后重新 capture_screen；仅生成截图不能证明程序正常。'
        # 动画/交互类页面：单帧截图证明不了"在动"，帧数不够或帧间零差异都是硬伤。
        if motion_required and motion_expected(path, scope):
            latest = captures[0]
            frames = int(latest.get('frames_captured') or 1)
            if frames < MOTION_MIN_FRAMES:
                return (f'【动态核验帧数不足】{path} 含动画/动画循环/canvas/视频，单帧无法证明它在动；'
                        f'请 capture_screen 传 frames={MOTION_MIN_FRAMES}~5 覆盖一个完整周期后重新核验。')
            if latest.get('dynamic_detected') is False:
                return (f'【动态核验无变化】{path} 连续 {frames} 帧之间没有像素差异'
                        f'（max_diff_ratio={latest.get("max_diff_ratio", 0)}），说明动画没有真正运行；'
                        '请检查动画循环、起播条件（是否要求用户先操作）并修正后重新截图；'
                        '若效果必须由用户操作触发，请先做成自动播放或让它在上电后立即运行。')
    return ''
