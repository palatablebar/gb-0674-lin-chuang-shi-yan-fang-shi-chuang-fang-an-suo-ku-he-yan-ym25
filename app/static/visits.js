const visitUi={busy:false};const feedback=document.querySelector('#feedback');document.querySelector('#source').value=new URL(location.href).searchParams.get('dataset')||document.querySelector('#source').value;document.querySelector('#run').addEventListener('click',async()=>{if(visitUi.busy)return;visitUi.busy=true;feedback.textContent='核验中…';try{const dataset=document.querySelector('#source').value;const r=await fetch(dataset===document.body.dataset.source?'/api/runs/'+document.body.dataset.runId+'/rerun':'/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dataset})});const data=await r.json();if(!r.ok)throw Error(data.detail);const url=new URL(location.href);url.searchParams.set('dataset',dataset);url.searchParams.set('run_id',data.manifest.run_id);location.href=url;}catch(e){feedback.textContent=e.message;}finally{visitUi.busy=false;}});
const filePicker=document.querySelector('#csv-file');
if(filePicker){
 const editor=document.querySelector('#csv-text');
 filePicker.addEventListener('change',async()=>{const file=filePicker.files[0];if(!file)return;if(file.size>2000000){editor.value='';alert('文件超过2MB，请缩小后导入');return;}const bytes=await file.arrayBuffer();try{editor.value=new TextDecoder('utf-8',{fatal:true}).decode(bytes);}catch(e){editor.value=new TextDecoder('gb18030',{fatal:true}).decode(bytes);}});
 document.querySelector('#import-csv').addEventListener('click',async event=>{event.target.disabled=true;try{const response=await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csv_text:editor.value})});const data=await response.json();if(!response.ok)throw Error(data.detail);location.href=location.pathname+'?run_id='+data.manifest.run_id;}catch(e){editor.setCustomValidity(e.message);editor.reportValidity();}finally{event.target.disabled=false;}});
 editor.addEventListener('input',()=>editor.setCustomValidity(''));
}
const signoffForm=document.querySelector('#signoff-form');
if(signoffForm){
 signoffForm.addEventListener('submit',async event=>{event.preventDefault();const button=signoffForm.querySelector('button');button.disabled=true;feedback.textContent='提交确认中…';try{
  const form=new FormData(signoffForm);
  const response=await fetch('/api/runs/'+signoffForm.dataset.run+'/signoff',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reviewer:form.get('reviewer'),opinion:form.get('opinion'),note:form.get('note')||'',basis_hash:signoffForm.dataset.basis,revision:Number(signoffForm.dataset.revision)})});
  const data=await response.json();
  if(!response.ok)throw Error(data.detail||'提交失败');
  feedback.textContent=(data.created_new?'已记录签署。':'该意见此前已提交，未新增签署。')+' 当前：'+data.status;
  location.reload();
 }catch(e){feedback.textContent=e.message;button.disabled=false;}});
}
