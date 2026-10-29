// Microbenchmark for the packaged MiniDb worker under installed Node and Bun.
// This runs the same bundled database/worker code under the installed runtimes,
// not inside either standalone executable.
import { build } from 'esbuild';
import { spawn } from 'node:child_process';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { performance } from 'node:perf_hooks';
const count=Number(process.env.WORKER_DOCS || 10000), runs=Number(process.env.BENCH_RUNS || 8), warmups=Number(process.env.BENCH_WARMUPS || 2);
if(process.platform!=='darwin'||!Number.isInteger(count)||count<4096||!Number.isInteger(runs)||runs<2||!Number.isInteger(warmups)||warmups<1)throw Error('macOS, WORKER_DOCS >= 4096, BENCH_RUNS >= 2 and BENCH_WARMUPS >= 1 required');
const dir=await mkdtemp(join(tmpdir(),'coderai-worker-bench-'));
try {
  const entry=join(dir,'benchmark.mjs');
  const code=`import {MiniDb} from ${JSON.stringify(resolve('packages/minidb/src/index.ts'))};
import {configureTextBuildWorkerRuntime} from ${JSON.stringify(resolve('packages/minidb/src/worker-runtime.ts'))};
configureTextBuildWorkerRuntime(${JSON.stringify(resolve('dist/text-build-worker.mjs'))});
const start=performance.now();const db=await MiniDb.open({dir:process.argv[2],durability:'relaxed',valueCodec:'json'});
await db.mset(Array.from({length:${count}},(_,i)=>['doc'+i,{text:'fixture searchable document '+i}]));const indexed=performance.now();
await db.createTextIndex('text',{fields:['text']});const finished=performance.now();const hits=db.search('text','searchable',{limit:10});if(hits.length!==10||hits.some(hit=>!hit.value?.text?.includes('searchable')))throw Error('index search returned incorrect results');const stats=typeof db.stats==='function'?db.stats():db.stats;await db.close();
console.log(JSON.stringify({setupMs:indexed-start,indexMs:finished-indexed,totalMs:performance.now()-start,stats}));`;
  await build({stdin:{contents:code,resolveDir:process.cwd(),sourcefile:'worker-benchmark.ts'},bundle:true,platform:'node',format:'esm',target:'node24',outfile:entry,logLevel:'silent'});
  const samples={node:[],bun:[]};
  for(let i=-warmups;i<runs;i++)for(const name of i%2===0?['bun','node']:['node','bun']) {
    process.stderr.write(`${i<0?'warmup':'run'} ${i<0?i+warmups+1:i+1} ${name}\n`);
    const db=join(dir,`${name}-${i}`), started=performance.now();
    const r=await new Promise((done,reject)=>{const c=spawn('/usr/bin/time',['-l',name,entry,db]);let out='',err='';c.stdout.on('data',x=>out+=x);c.stderr.on('data',x=>err+=x);c.on('error',reject);c.on('close',code=>done({code,out,err}));});
    if(r.code)throw Error(`${name} worker failed: ${r.err}`);
    const result=JSON.parse(r.out),rss=r.err.match(/(\d+)\s+maximum resident set size/), cpu=r.err.match(/([\d.]+) real\s+([\d.]+) user\s+([\d.]+) sys/);
    if(!(result.stats.textWorkerBuilds>0)||result.stats.textWorkerFallbacks!==0||!rss||!cpu)throw Error(`worker fallback or missing resource metrics: ${r.out} ${r.err}`);
    if(i>=0)samples[name].push({wallMs:performance.now()-started,setupMs:result.setupMs,indexMs:result.indexMs,totalMs:result.totalMs,peakRssKiB:Number(rss[1])/1024,userMs:Number(cpu[2])*1000,systemMs:Number(cpu[3])*1000});
  }
  const summary={};for(const [name,rows] of Object.entries(samples)){summary[name]={};for(const metric of Object.keys(rows[0])){const a=rows.map(r=>r[metric]).sort((x,y)=>x-y),q=p=>{const n=(a.length-1)*p,i=Math.floor(n);return a[i]+(a[Math.ceil(n)]-a[i])*(n-i);};summary[name][metric]={median:q(.5),p10:q(.1),p90:q(.9),min:a[0],max:a.at(-1)};}}
  const output=resolve(process.argv[2]||'benchmarks/results/worker-latest.json');await writeFile(output,JSON.stringify({count,runs,warmups,samples,summary},null,2)+'\n');console.log(JSON.stringify({output,count,runs,warmups,summary},null,2));
} finally {await rm(dir,{recursive:true,force:true});}
