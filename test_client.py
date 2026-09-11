"""Cliente real executado em Node, fetch offline."""
import subprocess


def test_client_contract():
    script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
(async () => {
  const client = await import('data:text/javascript;base64,' + fs.readFileSync('client.js').toString('base64'));
  let response = {session_id: 's', email: 'offline@example.test'}, status = 200, calls = [];
  global.fetch = async (url, options) => {
    calls.push([new URL(url), options]);
    return {ok: status === 200, status, json: async () => response};
  };
  await client.createEmail({provider:'other', domain:'random.com'});
  assert.deepEqual(JSON.parse(calls[0][1].body), {provider:'other', domain:'random.com', server:'1'});
  response = {state:'opened', verified:false};
  assert.equal((await client.getStatus('s+&%')).state, 'opened');
  assert.equal(calls.at(-1)[0].pathname, '/email/status');
  assert.equal(calls.at(-1)[0].searchParams.get('session_id'), 's+&%');
  response = {state:'ok', messages:[{mid:'m'}]};
  assert.equal((await client.checkEmail('s+&%', 1, 0)).messages[0].mid, 'm');
  assert.equal(calls.at(-1)[0].searchParams.get('session_id'), 's+&%');
  response = {best:'synthetic', error:''};
  assert.equal((await client.getBody('s', 'm+&%')).best, 'synthetic');
  assert.equal(calls.at(-1)[0].searchParams.get('mid'), 'm+&%');
  response = {error:'body failure'};
  await assert.rejects(client.getBody('s','m'), /body failure/);
  status = 503;
  await assert.rejects(client.createEmail(), /HTTP 503/);
  await assert.rejects(client.checkEmail('s'), /HTTP 503/);
  await assert.rejects(client.getBody('s','m'), /HTTP 503/);
})().catch(e => {console.error(e); process.exitCode = 1});
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
