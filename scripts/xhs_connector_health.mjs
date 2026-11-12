// Status transport must not collapse sandbox denials into an offline daemon.
export async function connectorStatus(requestDaemon) {
  let response;
  try { response=await requestDaemon('/status',{timeout:2000}); }
  catch(error) {
    const codes=[]; let current=error;
    for(let i=0;current && i<8;i++,current=current.cause) codes.push(current.code || current.name);
    if(codes.some(c=>['EPERM','EACCES'].includes(c))) throw new Error('CONNECTOR_PERMISSION_DENIED');
    if(codes.includes('ECONNREFUSED')) return null;
    if(codes.some(c=>['AbortError','TimeoutError','ETIMEDOUT','UND_ERR_CONNECT_TIMEOUT'].includes(c))) throw new Error('CONNECTOR_STATUS_TIMEOUT');
    throw new Error('CONNECTOR_STATUS_UNAVAILABLE');
  }
  if(!response.ok) throw new Error('CONNECTOR_STATUS_HTTP_ERROR');
  let status;
  try { status=await response.json(); } catch { throw new Error('CONNECTOR_STATUS_INVALID'); }
  if(!status || typeof status!=='object' || !status.daemonVersion || !Array.isArray(status.profiles)) throw new Error('CONNECTOR_STATUS_INVALID');
  return status;
}
export async function startConnector(probe,spawn,version,{attempts=25,sleep=ms=>new Promise(r=>setTimeout(r,ms))}={}) {
  const check=status=>{if(status && status.daemonVersion!==version) throw new Error('DAEMON_CHANGED_RECHECK_REQUIRED');return status;};
  let status=check(await probe());
  if(status) return status;
  // Only a positively observed connection refusal permits a single start.
  const child=spawn();
  let spawnFailed=false;
  child?.once?.('error',()=>{spawnFailed=true;});
  for(let i=0;i<attempts;i++) {
    await sleep(200);
    if(spawnFailed || (child?.exitCode!=null)) throw new Error('CONNECTOR_START_FAILED');
    status=check(await probe());
    if(status) return status;
  }
  throw new Error('CONNECTOR_START_FAILED');
}
