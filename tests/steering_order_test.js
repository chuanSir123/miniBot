// 用最小 DOM 桩验证「思考 / 引导 / 正文 / 工具」的追加顺序，
// 关键是引导气泡必须落在它生效的位置，而不是整轮末尾。
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '..', 'webui', 'index.html'), 'utf8');

function cut(startMarker, endMarker) {
  const i = src.indexOf(startMarker);
  if (i < 0) throw new Error('missing start marker: ' + startMarker);
  const j = src.indexOf(endMarker, i);
  if (j < 0) throw new Error('missing end marker: ' + endMarker);
  return src.slice(i, j);
}

const code = [
  cut('function blankTurnState() {', 'let currentSseSessionId = null;'),
  cut('function ensureTurnTraceBox() {', 'function handleToolDelta('),
  cut('function finishAssistantSegment() {', 'function reconcileFinalReply(')
].join('\n');

class El {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.className = '';
    this.dataset = {};
    this._html = '';
    this.style = {};
  }
  appendChild(c) { this.children.push(c); c.parent = this; return c; }
  insertBefore(c, ref) {
    const i = this.children.indexOf(ref);
    this.children.splice(i < 0 ? this.children.length : i, 0, c);
    return c;
  }
  querySelector(sel) { return this.findByClass(String(sel).replace(/^\./, '')); }
  querySelectorAll() { return []; }
  findByClass(cls) {
    for (const c of this.children) {
      if ((c.className || '').split(/\s+/).includes(cls)) return c;
      const inner = c.findByClass && c.findByClass(cls);
      if (inner) return inner;
    }
    return null;
  }
  set innerHTML(v) { this._html = v; }
  get innerHTML() { return this._html; }
  get firstChild() { return this.children[0] || null; }
  get classList() {
    const self = this;
    return {
      add: c => { if (!self.className.includes(c)) self.className += ' ' + c; },
      remove: c => { self.className = self.className.replace(' ' + c, ''); },
      toggle: () => {},
      contains: c => self.className.includes(c)
    };
  }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter(x => x !== this);
  }
}

const timeline = new El('div');
const document = {
  getElementById: id => (id === 'chat_timeline' ? timeline : new El('div')),
  createElement: t => new El(t),
  querySelectorAll: () => []
};

const sandbox = {
  document,
  timeline,
  escapeHtml: s => String(s == null ? '' : s),
  renderMarkdown: s => 'md:' + (s || ''),
  renderAssistantMarkdown: s => 'amd:' + (s || ''),
  renderEditSummaryCard: () => {},
  renderArtifactsGallery: () => {},
  smartScrollToBottom: () => {},
  updateRunningChangeSummary: () => {},
  setTaskState: () => {},
  setTaskPhase: () => {},
  showToast: () => {},
  loadWorkspaceFiles: () => {},
  Date,
  Math,
  console
};

const api = new Function(
  ...Object.keys(sandbox),
  code + `\nreturn {
    blankTurnState, beginThinkingBlock, endThinkingBlock, beginTextBlock,
    beginSteeringBlock, markSteeringApplied, ensureTurnTraceBox, finishAssistantSegment,
    turn: () => currentTurn,
    setTurn: v => { currentTurn = v; }
  };`
)(...Object.values(sandbox));

function layout() {
  return api.turn().group.children.map(c => c.className.split(/\s+/).filter(Boolean)[0]);
}

let failures = 0;
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) {
    failures += 1;
    console.log(`FAIL ${name}\n  expected ${JSON.stringify(expected)}\n  actual   ${JSON.stringify(actual)}`);
  } else {
    console.log(`ok   ${name} ${JSON.stringify(actual)}`);
  }
}

// 场景 1：思考中收到引导 —— 引导必须排在后续正文之前
api.setTurn(api.blankTurnState());
api.beginThinkingBlock();
api.beginSteeringBlock('别改标题，改成红色', false);
api.beginThinkingBlock();
api.beginTextBlock();
check('思考→引导→思考→正文', layout(), [
  'thinking-container', 'msg-user-container', 'thinking-container', 'msg-assistant-chunk'
]);
check('块类型', api.turn().blocks.map(b => b.kind), ['reasoning', 'steering', 'reasoning', 'text']);

// 场景 2：已有正文时收到引导 —— 旧正文收尾，引导插在它后面
api.setTurn(api.blankTurnState());
api.beginTextBlock();
api.finishAssistantSegment();
api.beginSteeringBlock('换个方案', false);
api.beginTextBlock();
check('正文→引导→正文', layout(), [
  'msg-assistant-chunk', 'msg-user-container', 'msg-assistant-chunk'
]);

// 场景 3：工具批次之间插入引导
api.setTurn(api.blankTurnState());
api.ensureTurnTraceBox();
api.beginSteeringBlock('先别跑测试', false);
api.ensureTurnTraceBox();
check('工具→引导→工具', layout(), ['turn-trace-box', 'msg-user-container', 'turn-trace-box']);

// 场景 4：引导应用后状态翻转
api.setTurn(api.blankTurnState());
api.beginThinkingBlock();
api.beginSteeringBlock('限定只改 webui', false);
check('未应用', api.turn().blocks[1].applied, false);
api.markSteeringApplied('限定只改 webui');
check('已应用', api.turn().blocks[1].applied, true);

// 场景 5：恢复时按 blocks 重建（含 steering 块）
const restored = api.blankTurnState();
api.setTurn(restored);

process.exit(failures ? 1 : 0);
