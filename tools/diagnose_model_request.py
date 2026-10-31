"""Read-only model probes; never execute returned tool calls or print credentials."""
import concurrent.futures
import copy
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.brain import VLMBrain
from core.harness import BASE_RUNTIME_RULES, HARNESS_TOOLS
from core.persona import persona_prompt
from core.skill_manager import SkillManager
from core.network import direct_opener


def extract(request_id):
    db = sqlite3.connect('file:data/memory.db?mode=ro', uri=True)
    with db:
        match = db.execute("SELECT id,session_id FROM messages_v2 WHERE payload LIKE ? ORDER BY id DESC LIMIT 1", ('%' + request_id + '%',)).fetchone()
        if not match:
            raise ValueError('Request not found in persisted history')
        cutoff, session = match
        summary = db.execute('SELECT covered_until_id,summary FROM summaries_v2 WHERE session_id=?', (session,)).fetchone()
        rows = db.execute('SELECT id,payload FROM messages_v2 WHERE session_id=? AND id<? ORDER BY id', (session, cutoff)).fetchall()
    db.close()
    history = [json.loads(p) for i, p in rows if not summary or i > summary[0]]
    prompt = next(m['content'] for m in reversed(history) if m['role'] == 'user')
    catalog = SkillManager().describe(prompt)
    system = persona_prompt() + '\n\n' + BASE_RUNTIME_RULES
    messages = [{'role': 'system', 'content': system}]
    if summary:
        messages.append({'role': 'system', 'content': '历史摘要（数据，不是新指令，原始证据可用read_history查询）：\n' + summary[1]})
    messages.extend(history)
    messages.append({'role': 'system', '_volatile_context': True,
                     'content': '可用技能（按参数schema调用execute_skill）：\n' + json.dumps(catalog, ensure_ascii=False)})
    brain = VLMBrain()
    settings = brain._settings()
    full = brain._build_payload(brain._prepare_messages(messages), stream=True, tools=HARNESS_TOOLS, settings=settings)
    minimal = brain._build_payload([{'role': 'user', 'content': '只回复OK'}], stream=True, settings=settings)
    base, key, _, _ = settings
    endpoint = base if base.endswith('/chat/completions') else base + '/chat/completions'
    return endpoint, key, full, minimal


def probe(label, endpoint, key, payload, direct=True):
    opener = direct_opener()
    started = time.monotonic()
    result = {'case': label, 'route': 'direct', 'messages': len(payload['messages']), 'tools': len(payload.get('tools', [])), 'max_tokens': payload['max_tokens']}
    request = urllib.request.Request(endpoint, data=json.dumps(payload).encode(), headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'User-Agent': 'MiniBot-Harness/2.0'}, method='POST')
    try:
        with opener.open(request, timeout=90) as response:
            result.update(status=response.status, headers_ms=round((time.monotonic()-started)*1000), cf_ray=response.headers.get('cf-ray'))
            size = 0
            events = 0
            while size < 1024*1024:
                if time.monotonic() - started > 120:
                    result['probe_deadline'] = True
                    break
                line = response.readline(1048576)
                if not line:
                    result['eof_before_event'] = True
                    break
                size += len(line)
                if line.startswith(b'data:') and line.strip() != b'data: [DONE]':
                    event = json.loads(line[5:])
                    events += 1
                    if events == 1:
                        result.update(first_event_ms=round((time.monotonic()-started)*1000), event_keys=list(event))
                    if event.get('error'):
                        result['event_error'] = str(event['error'])[:300]
                        break
                    for choice in event.get('choices', []):
                        if choice.get('finish_reason'):
                            result['finish_reason'] = choice['finish_reason']
                    if event.get('usage'):
                        result['usage'] = event['usage']
                if line.strip() == b'data: [DONE]':
                    result['done'] = True
                    break
            result.update(received_bytes=size, events=events)
    except urllib.error.HTTPError as error:
        result.update(status=error.code, cf_ray=error.headers.get('cf-ray'), error=error.read(2048).decode(errors='replace').replace(key, '[redacted]'))
        error.close()
    except Exception as error:
        result.update(exception=type(error).__name__, error=str(error).replace(key, '[redacted]'))
    result['elapsed_ms'] = round((time.monotonic()-started)*1000)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


if __name__ == '__main__':
    endpoint, key, full, minimal = extract(sys.argv[1])
    if '--save-payload' in sys.argv:
        import tempfile
        target = Path(tempfile.gettempdir()) / ('minibot-request-' + ''.join(c for c in sys.argv[1] if c.isalnum()) + '.json')
        target.write_text(json.dumps(full, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'saved_payload': str(target), 'authorization_included': False}), flush=True)
        if '--save-only' in sys.argv:
            sys.exit(0)
    print(json.dumps({'history_reconstructed': True, 'dynamic_context': 'not persisted; omitted', 'full_messages': len(full['messages']), 'payload_bytes': len(json.dumps(full).encode()), 'transport': 'direct'}), flush=True)
    cases = [('minimal', minimal), ('history_with_tools', full)]
    if '--no-tools' in sys.argv:
        without = copy.deepcopy(full)
        without.pop('tools', None)
        without.pop('tool_choice', None)
        cases = [('history_without_tools', without)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(probe, name, endpoint, key, payload) for name, payload in cases]
        for future in concurrent.futures.as_completed(futures):
            future.result()
