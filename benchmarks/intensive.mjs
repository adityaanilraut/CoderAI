// macOS/POSIX benchmark: node benchmarks/intensive.mjs NODE_BINARY BUN_BINARY [output.json]
import { fixture } from '../test/fixture.mjs';
import { Jimp } from 'jimp';
import { spawn, execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFile, writeFile, mkdir, stat, access } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { performance } from 'node:perf_hooks';
import os from 'node:os';

const [nodeArg, bunArg, outputArg = 'benchmarks/results/latest.json'] = process.argv.slice(2);
if (!nodeArg || !bunArg || process.platform !== 'darwin') throw Error('Provide Node and Bun executables; resource instrumentation currently requires macOS');
const binaries = [resolve(nodeArg), resolve(bunArg)];
const runs = Number(process.env.BENCH_RUNS || 10), warmups = Number(process.env.BENCH_WARMUPS || 2);
if (!Number.isInteger(runs) || runs < 2 || !Number.isInteger(warmups) || warmups < 1) throw Error('BENCH_RUNS >= 2 and BENCH_WARMUPS >= 1 required');
const timeoutMs = 45000;
const imageBytes = await new Jimp({ width: 2200, height: 1200, color: 0xffcc33ff }).getBuffer('image/png');
const mcpScript = `const readline=require('node:readline');readline.createInterface({input:process.stdin}).on('line',line=>{const m=JSON.parse(line);if(m.id===undefined)return;let result;if(m.method==='initialize')result={protocolVersion:m.params.protocolVersion,capabilities:{tools:{}},serverInfo:{name:'fixture',version:'1'}};else if(m.method==='tools/list')result={tools:[{name:'echo',description:'Echo text',inputSchema:{type:'object',properties:{text:{type:'string'}},required:['text']}}]};else if(m.method==='tools/call')result={content:[{type:'text',text:'MCP_REPLY_'+m.params.arguments.text}]};else result={};process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:m.id,result})+'\\n')});`;
const assert = (condition, message) => { if (!condition) throw Error(message); };
const execute = (bin, args, f, options = {}) => new Promise((done, reject) => {
  const start = performance.now();
  const command = options.plain ? bin : '/usr/bin/time';
  const commandArgs = options.plain ? args : ['-l', bin, ...args];
  const child = spawn(command, commandArgs, { cwd: f.cwd, env: f.env, stdio: ['pipe','pipe','pipe'] });
  let stdout = '', stderr = '', firstMs;
  child.stdout.on('data', c => { if (firstMs === undefined) firstMs = performance.now() - start; stdout += c; });
  child.stderr.on('data', c => stderr += c);
  const timer = setTimeout(() => { child.kill('SIGKILL'); reject(Error(`timeout: ${args.join(' ')}\n${stderr}`)); }, timeoutMs);
  child.on('error', error => { clearTimeout(timer); reject(error); });
  child.on('close', (code, signal) => {
    clearTimeout(timer);
    const time = stderr.match(/([\d.]+) real\s+([\d.]+) user\s+([\d.]+) sys/);
    const rss = stderr.match(/(\d+)\s+maximum resident set size/);
    done({ ms: performance.now() - start, firstMs, code, signal, stdout, stderr, ...(time && rss ? { userMs: +time[2]*1000, systemMs: +time[3]*1000, peakRssKiB: +rss[1]/1024 } : {}) });
  });
  child.stdin.end();
});
const events = r => r.stdout.trim().split('\n').map(s => JSON.parse(s));
const ok = (r, label) => assert(r.code === 0 && !r.signal && r.userMs !== undefined, `${label}: exit ${r.code} ${r.stderr}`);
const checkTurn = (r, label) => { ok(r,label); const ev=events(r); assert(ev.at(-1)?.exitCode===0, `${label}: unsuccessful final event`); return ev; };
async function cancellation(bin, f) {
  const start = performance.now();
  const child = spawn(bin, ['--trust','--yolo','--json','-p','SCENARIO cancel-shell'], { cwd:f.cwd,env:f.env,stdio:['pipe','pipe','pipe'] });
  let stdout='', stderr=''; child.stdout.on('data',x=>stdout+=x); child.stderr.on('data',x=>stderr+=x); child.stdin.end();
  const exited = new Promise((done,reject)=>{child.on('error',reject);child.on('close',(code,signal)=>done({code,signal}));});
  try {
    const path=join(f.cwd,'shell.pid'); const deadline=performance.now()+15000;
    while (performance.now()<deadline) { try { await access(path); break; } catch { await new Promise(r=>setTimeout(r,10)); } }
    const pid=Number(await readFile(path,'utf8')); assert(Number.isInteger(pid)&&pid>1,'invalid shell pid');
    const signalTime=performance.now(); child.kill('SIGTERM');
    const result=await Promise.race([exited,new Promise((_,reject)=>{const timer=setTimeout(()=>reject(Error('cancel timeout')),15000);timer.unref();})]);
    const cancelMs=performance.now()-signalTime;
    assert(result.code===130,`cancellation exit ${result.code}, ${stderr}`);
    let alive=true;try{process.kill(pid,0);}catch(error){if(error.code==='ESRCH')alive=false;else throw error;}
    assert(!alive,'cancelled shell still alive');
    assert(events({stdout}).at(-1)?.exitCode===130,'cancellation event missing');
    return { cancelMs, totalMs: performance.now()-start };
  } finally { if (child.exitCode===null) child.kill('SIGKILL'); }
}
function stats(values) {
  const a=[...values].sort((x,y)=>x-y), quantile=q=>{const p=(a.length-1)*q, i=Math.floor(p);return a[i]+(a[Math.ceil(p)]-a[i])*(p-i);};
  const median=quantile(.5), deviations=a.map(x=>Math.abs(x-median)).sort((x,y)=>x-y), m=deviations.length, mad=m%2?deviations[(m-1)/2]:(deviations[m/2-1]+deviations[m/2])/2;
  return Object.fromEntries(Object.entries({median,p10:quantile(.1),p90:quantile(.9),min:a[0],max:a.at(-1),mad}).map(([k,v])=>[k,+v.toFixed(2)]));
}
async function runIteration(bin, label) {
  const f=await fixture(), c=await fixture();
  try {
    const measured={};
    const measure=async (name,args,check) => {const r=await execute(bin,args,f);check(r);measured[name]={ms:r.ms,firstMs:r.firstMs,userMs:r.userMs,systemMs:r.systemMs,peakRssKiB:r.peakRssKiB};return r;};
    await writeFile(join(f.cwd,'large.png'),imageBytes);
    const mcpPath=join(f.root,'mcp.cjs');await writeFile(mcpPath,mcpScript);
    await measure('help',['--help'],r=>{ok(r,'help');assert(r.stdout.includes('Usage: coderai'),'help missing');});
    const simple=await measure('simple_turn',['--trust','--json','-p','Hello'],r=>{checkTurn(r,'simple turn');assert(r.stdout.includes('Hello from the deterministic provider.'),'simple output mismatch');});
    const id=events(simple).find(e=>e.type==='coderai.session')?.sessionId;assert(id,'no session ID');
    const largePrompt='long-context-marker '+('abcdefghij '.repeat(6000));
    await measure('large_prompt_66kb',['--trust','--json','-p',largePrompt],r=>{checkTurn(r,'large prompt');assert(JSON.stringify(f.requests.at(-1)).includes('long-context-marker'),'large prompt missing');});
    await measure('resume',['--resume',id,'--json','-p','follow-up'],r=>{const e=checkTurn(r,'resume');assert(e[0].sessionId===id,'wrong session resumed');});
    await measure('session_list',['sessions'],r=>{ok(r,'session list');assert(r.stdout.includes(id),'session not listed');});
    const exportPath=join(f.cwd,'session.md');
    await measure('session_export',['sessions','export',id,exportPath],r=>ok(r,'session export'));
    const exported=await readFile(exportPath,'utf8');assert(exported.includes('Hello')&&exported.includes('Hello from the deterministic provider.'),'export content mismatch');
    await measure('session_fork',['sessions','fork',id],r=>{ok(r,'fork');assert(r.stdout.trim() && r.stdout.trim()!==id,'fork missing');});
    const source=join(f.root,'import.jsonl');await writeFile(source,[{type:'session_meta',payload:{id:'fixture-session',cwd:f.cwd,source:'cli'}},{type:'response_item',payload:{type:'message',role:'user',content:[{type:'input_text',text:'Imported benchmark prompt'}]}},{type:'response_item',payload:{type:'message',role:'assistant',content:[{type:'output_text',text:'Imported benchmark answer'}]}}].map(x=>JSON.stringify(x)).join('\n')+'\n');
    await measure('session_import',['sessions','import','codex',source],r=>{ok(r,'import');assert(/complete|partial/.test(r.stdout),'import result missing');});
    await measure('tools_7',['--trust','--yolo','--json','-p','SCENARIO tools'],r=>{const ev=checkTurn(r,'tools');const results=ev.filter(e=>e.type==='tool.result');assert(results.length===7&&results.every(e=>!e.isError),'tool results mismatch');});
    assert(await readFile(join(f.cwd,'created.txt'),'utf8')==='second\n','tool file mismatch');
    await mkdir(join(f.cwd,'bulk'));await Promise.all(Array.from({length:300},(_,i)=>writeFile(join(f.cwd,'bulk',`file-${i}.ts`),`// benchmark file ${i}\n`+('const hello_value = 12345;\n'.repeat(80)))));
    await measure('tools_300_files',['--trust','--yolo','--json','-p','SCENARIO tools'],r=>{const ev=checkTurn(r,'bulk tools');const results=ev.filter(e=>e.type==='tool.result');assert(results.length===7&&results.every(e=>!e.isError),'bulk tool results mismatch');});
    assert(await readFile(join(f.cwd,'created.txt'),'utf8')==='second\n','bulk tool file mismatch');
    f.env.CODERAI_MODEL_CAPABILITIES='image_in';
    await measure('image_2200x1200',['--trust','--json','--attach','large.png','-p','Inspect image'],r=>{checkTurn(r,'image');const req=JSON.stringify(f.requests.at(-1));assert(req.includes('Image compressed to fit model limits')&&req.includes('media-originals')&&req.includes('data:image/'),'image request mismatch');});
    f.env.CODERAI_MODEL_CAPABILITIES='none';
    const idle=await execute('python3',[resolve('benchmarks/idle.py'),bin],{...f,env:{...f.env,TERM:'xterm-256color'}},{plain:true});
    assert(idle.code===0,`idle: ${idle.stderr}`);measured.idle={rssKiB:JSON.parse(idle.stdout).rssKiB};
    await mkdir(join(f.cwd,'.coderai'));await writeFile(join(f.cwd,'.coderai','mcp.json'),JSON.stringify({mcpServers:{fixture:{command:process.execPath,args:[mcpPath]}}}));
    await measure('mcp_turn',['--trust','--yolo','--json','-p','SCENARIO mcp'],r=>{checkTurn(r,'mcp');assert(r.stdout.includes('MCP_REPLY_mcp-test-value'),'MCP result mismatch');});
    measured.cancel=await cancellation(bin,c);
    return measured;
  } finally { await Promise.all([f.cleanup(),c.cleanup()]); }
}
const metadata={date:new Date().toISOString(),platform:os.platform(),release:os.release(),arch:os.arch(),cpu:os.cpus()[0].model,memoryBytes:os.totalmem(),node:process.version,bun:execFileSync('bun',['--version'],{encoding:'utf8'}).trim(),runs,warmups,projectCommit:execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8'}).trim(),dirty:!!execFileSync('git',['status','--porcelain'],{encoding:'utf8'}).trim(),binaries:[]};
for(const path of binaries) metadata.binaries.push({path,bytes:(await stat(path)).size,sha256:createHash('sha256').update(await readFile(path)).digest('hex')});
const samples=[[],[]];
for(let i=-warmups;i<runs;i++) for(const index of i%2===0?[1,0]:[0,1]) {
  const label=index?'bun':'node';process.stderr.write(`${i<0?'warmup':'run'} ${i<0?i+warmups+1:i+1}/${i<0?warmups:runs} ${label}\n`);
  const result=await runIteration(binaries[index],label);if(i>=0)samples[index].push(result);
}
const summaries=samples.map(group=>Object.fromEntries(Object.keys(group[0]).map(name=>[name,Object.fromEntries(Object.keys(group[0][name]).map(metric=>[metric,stats(group.map(r=>r[name][metric]))]))])));
const result={metadata,summaries:{node:summaries[0],bun:summaries[1]},samples:{node:samples[0],bun:samples[1]}};
await mkdir(resolve(outputArg,'..'),{recursive:true});await writeFile(resolve(outputArg),JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify({output:resolve(outputArg),metadata,summaries:result.summaries},null,2));
