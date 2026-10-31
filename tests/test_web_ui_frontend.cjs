// Run with: node --test tests/test_web_ui_frontend.cjs
// Exercise the actual inline UI script without contacting a model or editing files.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../webui/index.html'), 'utf8')
  .split('<script>')[1].split('</script>')[0];

function harness() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value:'', textContent:'', innerHTML:'', disabled:false, readOnly:false,
      style:{}, children:[], scrollHeight:56,
      classList:{add(){}, remove(){}, toggle(){}, contains(){return false;}},
      focus(){}, setAttribute(){}, querySelector(){return null;}, querySelectorAll(){return [];}
    });
    return elements.get(id);
  }
  const calls = [];
  const context = vm.createContext({
    document:{getElementById:element, querySelector:()=>element('query'), querySelectorAll:()=>[],
      body:element('body'), addEventListener(){}},
    window:{addEventListener(){}}, localStorage:{getItem(){return null;},setItem(){}},
    setInterval(){},clearInterval(){},setTimeout(){},console,confirm:()=>true,
    fetch:async(url, options)=>{
      calls.push({url,options});
      return {ok:true,json:async()=>({success:true})};
    }
  });
  vm.runInContext(source,context);
  vm.runInContext(`
    const actualSetTaskState = setTaskState;
    setTaskState = running => {isTaskRunning = running;};
    showToast = message => {document.getElementById('toast').textContent = message;};
    smartScrollToBottom = () => {};
    loadWorkspaceFiles = async () => {};
    loadSessions = async () => {};
  `,context);
  return {context,element,calls,run:code=>vm.runInContext(code,context)};
}

test('failed historical rollback preserves editing state and never submits chat',async()=>{
  const h=harness();
  h.element('prompt_input').value='修改后的任务';
  h.run("editingMessageId = 12; currentAttachedImage = 'data:image/png;base64,AA==';");
  h.context.fetch=async url=>{
    h.calls.push({url});
    return {ok:true,json:async()=>({success:false,error:'checkpoint conflict'})};
  };
  await h.run('submitMessage()');
  assert.deepEqual(h.calls.map(c=>c.url),['/api/session/rollback']);
  assert.equal(h.element('prompt_input').value,'修改后的任务');
  assert.equal(h.run('editingMessageId'),12);
  assert.equal(h.run('currentAttachedImage'),'data:image/png;base64,AA==');
  assert.equal(h.element('btn_submit').disabled,false);
});

test('HTTP failure retains text and attachment and restores the send control',async()=>{
  const h=harness();
  h.element('prompt_input').value='任务草稿';
  h.run("currentAttachedImage = 'data:image/png;base64,AA==';");
  h.context.fetch=async()=>({ok:false,status:503,json:async()=>({error:'unavailable'})});
  await h.run('submitMessage()');
  assert.equal(h.element('prompt_input').value,'任务草稿');
  assert.equal(h.run('currentAttachedImage'),'data:image/png;base64,AA==');
  assert.equal(h.run('isTaskRunning'),false);
  assert.equal(h.element('prompt_input').readOnly,false);
  assert.match(h.element('toast').textContent,/unavailable/);
});

test('double send is suppressed while the original request is in flight',async()=>{
  const h=harness();
  h.element('prompt_input').value='一次任务';
  let resolve;
  h.context.fetch=(url,options)=>{
    h.calls.push({url,options});
    return new Promise(done=>{resolve=done;});
  };
  const first=h.run('submitMessage()');
  await h.run('submitMessage()');
  assert.equal(h.calls.length,1);
  resolve({ok:true,json:async()=>({success:true})});
  await first;
  assert.equal(h.element('prompt_input').value,'');
  assert.equal(h.element('btn_submit').disabled,false);
});

test('image-only chat sends its image and clears it only after acceptance',async()=>{
  const h=harness();
  h.run("currentAttachedImage = 'data:image/png;base64,AA==';");
  await h.run('submitMessage()');
  const payload=JSON.parse(h.calls[0].options.body);
  assert.equal(h.calls[0].url,'/api/chat');
  assert.deepEqual(payload.input_images,['data:image/png;base64,AA==']);
  assert.equal(h.run('currentAttachedImage'),null);
  assert.equal(h.calls[0].options.headers['X-MiniBot-Request'],'1');
});

test('unsupported image steering retains both the image and the message',async()=>{
  const h=harness();
  h.element('prompt_input').value='参考这张图';
  h.run("isTaskRunning = true; currentAttachedImage = 'data:image/png;base64,AA==';");
  await h.run('submitMessage()');
  assert.equal(h.calls.length,0);
  assert.equal(h.element('prompt_input').value,'参考这张图');
  assert.equal(h.run('currentAttachedImage'),'data:image/png;base64,AA==');
  assert.equal(h.run('isTaskRunning'),true);
});

test('failed steering preserves its text and the active task',async()=>{
  const h=harness();
  h.element('prompt_input').value='改用浅色';
  h.run('isTaskRunning = true;');
  h.context.fetch=async()=>({ok:true,json:async()=>({success:false,message:'no active turn'})});
  await h.run('submitMessage()');
  assert.equal(h.element('prompt_input').value,'改用浅色');
  assert.equal(h.run('isTaskRunning'),true);
});

test('successful rollback is not repeated if the following chat request fails',async()=>{
  const h=harness();
  h.element('prompt_input').value='编辑后重发';
  h.run('editingMessageId = 8; loadSessionMessages = async () => {};');
  h.context.fetch=async url=>{
    h.calls.push({url});
    return {ok:true,json:async()=>url.endsWith('rollback') ? {success:true} : {success:false,error:'offline'}};
  };
  await h.run('submitMessage()');
  assert.deepEqual(h.calls.map(c=>c.url),['/api/session/rollback','/api/chat']);
  assert.equal(h.run('editingMessageId'),null);
  assert.equal(h.element('prompt_input').value,'编辑后重发');
});

test('stop button calls the cancellation API for the captured session',async()=>{
  const h=harness();
  h.run('isTaskRunning = true; loadSessionMessages = async () => {};');
  await h.run('cancelCurrentTask()');
  assert.equal(h.calls[0].url,'/api/cancel');
  assert.equal(JSON.parse(h.calls[0].options.body).session_id,'web:main:default');
  assert.equal(h.run('isTaskRunning'),false);
  assert.equal(h.element('btn_cancel_task').disabled,false);
});

test('Chinese IME confirmation does not submit a message',()=>{
  const h=harness();
  h.element('prompt_input').value='正在输入中文';
  let prevented=false;
  h.context.keyEvent={key:'Enter',isComposing:true,preventDefault(){prevented=true;}};
  h.run('handleComposerKey(keyEvent)');
  assert.equal(h.calls.length,0);
  assert.equal(prevented,false);
});

test('late history response from a previous session cannot overwrite the current view',async()=>{
  const h=harness();
  let resolve;
  h.context.fetch=()=>new Promise(done=>{resolve=done;});
  const pending=h.run("loadSessionMessages('web:main:default')");
  h.run("currentSessionId = 'web:main:other';");
  h.element('chat_timeline').innerHTML='新会话内容';
  resolve({ok:true,json:async()=>({success:true,data:[],is_active:false})});
  await pending;
  assert.equal(h.element('chat_timeline').innerHTML,'新会话内容');
});

test('repeated streaming state updates preserve elapsed time and the current phase',()=>{
  const h=harness();
  h.context.Date={now:()=>120000};
  h.run('updateLiveActivityUI = () => {}; currentTurn.startTime = 100000; setTaskState = actualSetTaskState;');
  h.run("setTaskState(true, '正在读取文件');");
  h.context.Date.now=()=>130000;
  h.run('setTaskState(true);');
  assert.equal(h.run('taskStartTime'),100000);
  assert.equal(h.run('currentTaskPhase'),'正在读取文件');
  assert.equal(h.run('formatElapsed(632)'),'10分钟 32秒');
});

function fixtureConfig() {
  return {
    vlm:{model:'qa-model',base_url:'http://localhost/v1',context_window:8192,max_tokens:512,
      enable_thinking:false,reasoning_effort:'none',models:[{id:'qa',model:'qa-model',name:'QA',is_default:true,provider_option:'keep-me'}]},
    jev:{base_url:'http://localhost/decision',model:'decision'},
    persona:{name:'MiniBot',aliases:['bot'],prompt:'实际生效的人设',system_prompt:'旧字段'},
    channels:{pet:{enabled:false,scale:1,pet_dir:'assets/test'},napcat:{enabled:false,group_smart_reply:true,smart_reply:false}},
    runtime:{max_workers:2}
  };
}

async function loadFixtureConfig(h) {
  const cfg=fixtureConfig();
  h.run('renderModelsList = () => {}; loadAvailableModels = async () => {};');
  h.context.fetch=async(url,options)=>{
    h.calls.push({url,options});
    return {ok:true,json:async()=>options?.method === 'POST' ? {success:true} : cfg};
  };
  await h.run('loadConfig()');
  return cfg;
}

test('settings load/save uses the persona and QQ fields read by the backend',async()=>{
  const h=harness();
  await loadFixtureConfig(h);
  assert.equal(h.element('persona_prompt').value,'实际生效的人设');
  assert.equal(h.element('napcat_smart_reply').checked,true);
  h.element('persona_prompt').value='更新后的人设';
  h.element('napcat_smart_reply').checked=false;
  await h.run('saveFullConfig()');
  const saved=JSON.parse(h.calls.find(c=>c.options?.method === 'POST').options.body);
  assert.equal(saved.persona.prompt,'更新后的人设');
  assert.equal(saved.channels.napcat.group_smart_reply,false);
  assert.equal(saved.vlm.models[0].provider_option,'keep-me');
});

test('untouched form fields do not overwrite direct JSON changes',async()=>{
  const h=harness();
  const cfg=await loadFixtureConfig(h);
  const edited=structuredClone(cfg);
  edited.vlm.max_tokens=1024;
  edited.vlm.models[0].provider_option='edited-in-json';
  edited.persona.prompt='JSON 中修改的人设';
  edited.runtime.max_workers=4;
  h.element('advanced_config').value=JSON.stringify(edited);
  await h.run('saveFullConfig()');
  const saved=JSON.parse(h.calls.find(c=>c.options?.method === 'POST').options.body);
  assert.equal(saved.vlm.max_tokens,1024);
  assert.equal(saved.vlm.models[0].provider_option,'edited-in-json');
  assert.equal(saved.persona.prompt,'JSON 中修改的人设');
  assert.equal(saved.runtime.max_workers,4);
});

test('invalid advanced JSON is rejected before any configuration write',async()=>{
  const h=harness();
  await loadFixtureConfig(h);
  h.element('advanced_config').value='[]';
  await h.run('saveFullConfig()');
  assert.equal(h.calls.filter(c=>c.options?.method === 'POST').length,0);
  assert.match(h.element('toast').textContent,/JSON 对象/);
});

test('code fences retain literal newlines and Markdown punctuation',()=>{
  const h=harness();
  h.context.markdown='```html\n<div>**literal**</div>\n<img src="x">\n```';
  const html=h.run('renderMarkdown(markdown)');
  assert.match(html,/<pre><code>&lt;div&gt;\*\*literal\*\*&lt;\/div&gt;\n/);
  assert.equal(html.includes('<br>'),false);
  assert.equal(html.includes('<strong>'),false);
  assert.equal(html.includes('<img'),false);
});

test('partial streaming code blocks and inline code remain readable and literal',()=>{
  const h=harness();
  h.context.markdown='说明\n```js\nconst x = 1;\nconst y = 2;';
  assert.match(h.run('renderMarkdown(markdown)'),/说明<br><pre><code>const x = 1;\nconst y = 2;<\/code><\/pre>/);
  h.context.markdown='`**literal**`';
  assert.equal(h.run('renderMarkdown(markdown)'),'<code>**literal**</code>');
});

test('image links do not create executable URLs',()=>{
  const h=harness();
  h.context.markdown='![图片](javascript:alert)';
  assert.equal(h.run('renderMarkdown(markdown)'),'图片');
});

test('diff line numbers follow each hunk, additions and deletions',()=>{
  const h=harness();
  h.context.diff='--- a/app.py\n+++ b/app.py\n@@ -10,2 +20,2 @@\n same\n-old\n+new\n@@ -40 +60 @@\n-before\n+after';
  h.context.output={innerHTML:''};
  h.run('renderDiffLines(diff, output)');
  assert.match(h.context.output.innerHTML,/data-old-line="10" data-new-line="20"/);
  assert.match(h.context.output.innerHTML,/data-old-line="11" data-new-line=""/);
  assert.match(h.context.output.innerHTML,/data-old-line="" data-new-line="21"/);
  assert.match(h.context.output.innerHTML,/data-old-line="40" data-new-line=""/);
  assert.match(h.context.output.innerHTML,/data-old-line="" data-new-line="60"/);
});

test('SSE deduplicates message IDs without dropping repeated prompt text',()=>{
  const h=harness();
  const callbacks={};
  h.context.EventSource=class {
    static CLOSED=2;
    readyState=1;
    addEventListener(name,callback){callbacks[name]=callback;}
    close(){}
  };
  h.context.messages=[];
  h.run('appendMessageElement = message => messages.push(message); setupSSE(currentSessionId);');
  h.element('chat_timeline').querySelectorAll=()=>[{dataset:{msgId:'1',msgText:'相同的任务'},textContent:'相同的任务'}];
  callbacks.user_message({data:JSON.stringify({id:2,content:'相同的任务'})});
  assert.equal(h.context.messages.length,1);
  callbacks.user_message({data:JSON.stringify({id:1,content:'相同的任务'})});
  assert.equal(h.context.messages.length,1);
});

test('tool boundaries do not slice text using a previous model call length',()=>{
  const h=harness();
  h.context.previous={innerHTML:''};
  h.context.document.createElement=()=>({className:''});
  h.run("currentTurn.group = {appendChild(){}}; currentTurn.activeTextEl = previous;");
  h.context.chunk={delta:'先创建一个文件。\n\n',full:'先创建一个文件。\n\n'};
  h.run('consumeAssistantDelta(chunk); ensureTurnTraceBox();');
  assert.equal(h.context.previous.innerHTML,'先创建一个文件。');
  h.context.chunk={delta:'现在',full:'现在'};
  assert.equal(h.run('consumeAssistantDelta(chunk)'),'现在');
  h.context.chunk={delta:'已经动起来了！\n\n',full:'现在已经动起来了！\n\n'};
  assert.equal(h.run('renderAssistantMarkdown(consumeAssistantDelta(chunk))'),'现在已经动起来了！');
});

test('assistant display removes outer blank lines without changing stored text or fenced code',()=>{
  const h=harness();
  h.context.text='\n\n正文\n第二行\n\n';
  assert.equal(h.run('renderAssistantMarkdown(text)'),'正文<br>第二行');
  assert.equal(h.context.text,'\n\n正文\n第二行\n\n');
  h.context.text='```py\nfirst\n\nsecond\n```\n\n';
  assert.equal(h.run('renderAssistantMarkdown(text)'),'<pre><code>first\n\nsecond\n</code></pre>');
});

test('completion reconciles the full final reply when streaming missed its prefix',()=>{
  const h=harness();
  h.context.finalElement={innerHTML:'尾部'};
  h.run('currentTurn.activeTextEl = finalElement; currentTurn.group = {};');
  h.context.reply='完整的最终回复\n\n';
  h.run('reconcileFinalReply(reply)');
  assert.equal(h.context.finalElement.innerHTML,'完整的最终回复');
  assert.equal(h.run('currentTurn.segmentText'),'完整的最终回复\n\n');
});

test('effort slider uses all four original request values and updates the compact label',()=>{
  const h=harness();
  h.run("_availableModelsCache = [{id:'qa',name:'QA Model'}];");
  h.element('model_selector').value='qa';
  for (const [index,value,label] of [[0,'none','不思考'],[1,'low','轻度'],[2,'medium','标准'],[3,'high','深度']]) {
    h.run(`selectThinkingLevel(${index})`);
    assert.equal(h.element('thinking_selector').value,value);
    assert.equal(h.element('model_picker_effort').textContent,label);
    assert.equal(h.element('effort_slider').value,String(index));
  }
});
