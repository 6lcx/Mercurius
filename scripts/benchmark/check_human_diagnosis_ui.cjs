// Synthetic DOM/clock smoke test only. Does not save or submit human observations.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync('benchmarks/templates/human_diagnosis.html','utf8');
const source=html.match(/<script>([\s\S]*?)<\/script>/)[1];
async function check(group){
  const nodes={};let tick=0,blob;
  const data={protocol_hash:'synthetic-test-only',logs:'synthetic logs',cases:Array.from({length:6},(_,i)=>({case_id:'D'+i,request_id:'R'+i,assisted:{}}))};
  const get=id=>nodes[id]??(nodes[id]={hidden:false,value:'',checked:false,textContent:'',querySelector:()=>({textContent:''})});
  get('data').textContent=JSON.stringify(data);get('participant').value='SYNTHETIC-NOT-HUMAN';get('group').value=group;get('attest').checked=true;
  const storage=new Map();
  const sandbox={document:{getElementById:get,addEventListener:()=>{},createElement:()=>({click(){}})},
    localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},
    window:{addEventListener:()=>{}},performance:{now:()=>tick+=100},Date,JSON,Blob,
    URL:{createObjectURL:b=>(blob=b,'test-only'),revokeObjectURL:()=>{}},setTimeout:fn=>fn()};
  vm.runInNewContext(source,sandbox);
  get('begin').onclick();
  for(let i=0;i<6;i++){
    get('start').onclick();get('component').value='unknown';get('category').value='unknown';get('submit').onclick();
  }
  assert.equal(get('done').hidden,false);get('download').onclick();
  const record=JSON.parse(await blob.text());
  assert.equal(record.interface_version,'guided-zh-v2');
  assert.equal(record.answers.length,6);assert.equal(record.complete,true);
  assert.equal(record.answers[0].condition,group==='A'?'logs':'assisted');
  assert.equal(record.answers.filter(a=>a.condition==='assisted').length,3);
  assert(record.answers.every(a=>a.elapsed_ms===100));
  // A reload must recover saved answers for export, not reopen timed cases.
  vm.runInNewContext(source,{...sandbox});get('begin').onclick();get('download').onclick();
  assert.equal(JSON.parse(await blob.text()).answers.length,6);
}
(async()=>{await check('A');await check('B');console.log('Synthetic UI checks passed; no human records saved.');})().catch(e=>{console.error(e);process.exitCode=1});
