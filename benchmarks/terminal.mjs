import { fixture } from '../test/fixture.mjs';
import { spawn } from 'node:child_process';
import { writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const bins=process.argv.slice(2,4).map(x=>resolve(x)), out=resolve(process.argv[4]||'benchmarks/results/terminal-latest.json');
if(bins.length!==2)throw Error('Provide Node and Bun executables');
const runs=Number(process.env.BENCH_RUNS||8),warmups=Number(process.env.BENCH_WARMUPS||2),samples={node:[],bun:[]};
for(let i=-warmups;i<runs;i++)for(const index of i%2===0?[1,0]:[0,1]){
  const name=index?'bun':'node';process.stderr.write(`${i<0?'warmup':'run'} ${i<0?i+warmups+1:i+1} ${name}\n`);const f=await fixture();
  try {
    const result=await new Promise((done,reject)=>{const c=spawn('python3',[resolve('benchmarks/terminal.py'),bins[index]],{cwd:f.cwd,env:{...f.env,TERM:'xterm-256color'}});let out='',err='';c.stdout.on('data',x=>out+=x);c.stderr.on('data',x=>err+=x);c.on('error',reject);c.on('close',code=>code?reject(Error(err)):done(JSON.parse(out)));});
    if(!result.terminalRestored||!f.requests.some(r=>JSON.stringify(r.messages).includes('hello')))throw Error('Terminal result mismatch');
    if(i>=0)samples[name].push(result);
  } finally {await f.cleanup();}
}
const summary={};for(const [name,rows] of Object.entries(samples)){summary[name]={};for(const metric of ['startupIdleMs','turnAnswerMs','quitMs']){const a=rows.map(r=>r[metric]).sort((x,y)=>x-y),q=p=>{const n=(a.length-1)*p,i=Math.floor(n);return a[i]+(a[Math.ceil(n)]-a[i])*(n-i);};summary[name][metric]={median:q(.5),p10:q(.1),p90:q(.9),min:a[0],max:a.at(-1)};}}
await writeFile(out,JSON.stringify({runs,warmups,samples,summary},null,2)+'\n');console.log(JSON.stringify({out,runs,warmups,summary},null,2));
