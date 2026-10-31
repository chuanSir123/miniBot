"""Opt-in live model benchmark. Never executes returned tools or sends QQ messages."""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.brain import VLMBrain
from core.jev import JevClient
from core.harness import HARNESS_TOOLS, BASE_RUNTIME_RULES
from core.persona import persona_prompt


def model(brain, messages):
    started = time.monotonic()
    first = None
    stream = brain.stream_chat_and_tools(messages, tools=HARNESS_TOOLS)
    error = None
    while True:
        try:
            kind, value = next(stream)
            if kind == 'text_delta' and first is None:
                first = time.monotonic() - started
            if kind == 'error':
                error = str(value)[:200]
        except StopIteration as done:
            text, calls, usage = done.value or ('', [], {})
            break
    return {'elapsed_sec': round(time.monotonic() - started, 3),
            'first_text_sec': round(first, 3) if first is not None else None,
            'text': text, 'calls': calls, 'usage': usage, 'error': error}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--output', default='data/response-chain-benchmark.json')
    parser.add_argument('--cache-only', action='store_true')
    parser.add_argument('--hybrid-only', action='store_true')
    parser.add_argument('--timeout', type=int, default=30)
    args = parser.parse_args()
    import core.brain as brain_module
    probe_config = brain_module.load_config()
    probe_config['runtime']['request_timeout_sec'] = args.timeout
    brain_module.load_config = lambda: probe_config
    brain, jev = VLMBrain(), JevClient()
    settings = brain._settings()
    # Bound generation only for this probe; never modifies config.json.
    brain._settings = lambda: (*settings[:3], dict(settings[3], max_tokens=256))
    cases = [('chat', '今天有点累，陪我聊两句吧，不需要调用工具。'),
             ('noarg', '请列出当前已安装的技能。'),
             ('parameterized', '请读取 data/benchmark_fixture.txt 文件内容。')]
    rows = []
    system = persona_prompt() + '\n\n' + BASE_RUNTIME_RULES
    if args.cache_only:
        history = []
        for i in range(12):
            history += [{'role': 'user', 'content': f'第{i}次讨论：我们只讨论测试用的上下文排列，不执行任何实际操作。' * 12},
                        {'role': 'assistant', 'content': '了解，我们保留已确认的信息，后续根据新问题继续讨论。' * 12}]
        for layout in ('front', 'tail'):
            for step in range(1 if layout == 'front' else 3):
                dynamic = {'role': 'system', '_volatile_context': layout == 'tail', 'content': f'动态时间和状态：{layout}-{step}。本次只回复“收到”，不要调用工具。'}
                messages = [{'role': 'system', 'content': system}]
                messages += ([dynamic] + history if layout == 'front' else history + [dynamic])
                messages += [{'role': 'user', 'content': '只回复收到。'}]
                row = {'layout': layout, 'step': step, **model(brain, messages)}
                rows.append(row)
                Path(args.output).write_text(json.dumps({'model': brain.model, 'cache_probe': rows}, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps(row, ensure_ascii=False), flush=True)
        return
    for repeat in range(args.repeats):
        for case, prompt in cases:
            for mode in (('hybrid',) if args.hybrid_only else ('direct', 'jev_route', 'jev_noarg')):
                start = time.monotonic()
                route, jev_sec = None, 0
                if mode != 'direct':
                    t = time.monotonic()
                    if mode == 'hybrid':
                        route = jev.select_fixed_action(prompt, context={})
                    elif mode == 'jev_route':
                        route = jev.route_request(prompt, [], context={})
                    else:
                        route = jev.decide(json.dumps({'user': prompt}, ensure_ascii=False), {
                            'route': {'type': 'choice', 'instructions': '仅明确请求列出已安装技能时选择list_skills；其他请求选vlm，禁止猜参数。',
                                      'options': ['list_skills', 'vlm']}}).get('route', 'vlm')
                    jev_sec = time.monotonic() - t
                messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}]
                if mode in ('jev_noarg', 'hybrid') and route == 'list_skills':
                    result = {'elapsed_sec': 0, 'first_text_sec': None, 'text': '', 'calls': [{
                        'id': 'benchmark', 'type': 'function', 'function': {'name': 'list_skills', 'arguments': '{}'}}], 'usage': {}, 'error': None}
                else:
                    result = model(brain, messages)
                calls = result['calls']
                final = None
                if calls and not result['error']:
                    messages.append({'role': 'assistant', 'content': result['text'] or None, 'tool_calls': calls})
                    for call in calls:
                        # Fixtures only. No file read, command, network tool, or grant is executed.
                        name = call.get('function', {}).get('name')
                        fixture = {'ok': True, 'skills': [], 'total': 0} if name == 'list_skills' else {'ok': True, 'output': 'benchmark fixture content'}
                        messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(fixture)})
                    final = model(brain, messages)
                expected = {'noarg': 'list_skills', 'parameterized': 'read_file'}.get(case)
                names = [c.get('function', {}).get('name') for c in calls]
                row = {'repeat': repeat, 'case': case, 'mode': mode, 'route': route,
                       'jev_sec': round(jev_sec, 3), 'total_sec': round(time.monotonic() - start, 3),
                       'expected_tool': expected, 'tool_match': names == [expected] if expected else not calls,
                       'first': result, 'final': final}
                rows.append(row)
                Path(args.output).write_text(json.dumps({'model': brain.model, 'rows': rows}, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps({k: v for k, v in row.items() if k not in ('first', 'final')}, ensure_ascii=False), flush=True)
    for mode in (('hybrid',) if args.hybrid_only else ('direct', 'jev_route', 'jev_noarg')):
        subset = [r for r in rows if r['mode'] == mode]
        print(mode, 'median_sec', round(statistics.median(r['total_sec'] for r in subset), 3),
              'tool_match', sum(r['tool_match'] for r in subset), '/', len(subset))


if __name__ == '__main__':
    main()
