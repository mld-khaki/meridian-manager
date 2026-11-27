"""Exercise the embedded Server UI without SSH or phone operations."""
import ast
import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest


def ui_parts():
    tree = ast.parse((Path(__file__).resolve().parent.parent / "phone-proxy-web.py").read_text())
    page = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(a, ast.Name) and a.id == "PAGE" for a in n.targets))
    helpers = page[page.index("let lastADB="):page.index("function svc(")]
    helpers += page[page.index("function tab(w)"):page.index("// Cockpit:")]
    return page, helpers


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.ids, self.buttons = [], {}, []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        key = attrs.get("id")
        if key:
            assert key not in self.ids, "Duplicate DOM id: " + key
            self.ids[key] = {"parent": next((k for _, k in reversed(self.stack) if k), None),
                             "value": attrs.get("value", ""), "hidden": "hidden" in attrs}
        if tag == "button" and "data-adb" in attrs:
            self.buttons.append(attrs["data-adb"])
        if tag not in ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"):
            self.stack.append((tag, key))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


def test_server_reuses_adb_controls_independently_of_ssh_page():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise Server tab behavior.")
    page, helpers = ui_parts()
    dom = Elements()
    dom.feed(page)
    script = """
const assert=require('node:assert/strict');
const ids=DOM;
for(const [id,element] of Object.entries(ids)){
 element.id=id;element.dataset={};element.textContent='';element.innerHTML='';
 element.parentElement=ids[element.parent];element.addEventListener=()=>{};
 element.appendChild=child=>{child.parentElement=element};
 element.removeAttribute=key=>{delete element[key]};
}
const controls=OPS.map(adb=>({dataset:{adb}}));
const document={activeElement:null,getElementById:id=>ids[id],
 querySelectorAll:selector=>selector.includes('adb-brightness-set')?controls.concat(ids['adb-brightness-set']):controls};
const setInterval=()=>{},setTimeout=()=>{};
const esc=s=>String(s);
const calls=[];
const state={device:'phone',label:'Test phone',endpoint:'127.0.0.1:40000',on:true,connected:true,
 adb_installed:true,scrcpy_installed:true,scrcpy_running:false,battery:{percent:72,status:'charging'},
 tailscale:{installed:true,process_running:true,vpn_active:true},capabilities:{tailscale_on:{},tailscale_off:{}}};
let resolveServer,serverReply=null;
const api=async(path,options)=>{
 calls.push([path,options]);
 if(path==='/api/adb')return options?{ok:true,message:'Sent',state}:state;
 if(path==='/api/device')return {active:'phone',devices:{phone:{label:'Test phone',root:false}}};
 if(path==='/api/tabctl')return serverReply||new Promise(resolve=>{resolveServer=resolve});
 throw new Error('Unexpected API: '+path);
};
""".replace("DOM", json.dumps(dom.ids)).replace("OPS", json.dumps(dom.buttons)) + helpers + """
(async()=>{
 await adbLoad();
 const card=ids['adb-card'],address=ids['adb-address'],pair=ids['adb-pair'];
 address.value='127.0.0.1:40000';address.dataset.edited='1';
 pair.value='127.0.0.1:41000';pair.dataset.edited='1';ids['adb-code'].value='123456';
 tab('p');await new Promise(setImmediate);
 assert.equal(card.parentElement,ids['server-adb'],'Server must expose ADB before SSH finishes');
 assert.match(ids['adb-info'].innerHTML,/72/);
 assert.match(ids['devnote'].textContent,/scrcpy/);
 assert.ok(resolveServer,'SSH page is still pending');
 assert.equal(controls.find(b=>b.dataset.adb==='wake').disabled,false);
 await adbOp('wake');
 const command=calls.find(([path,options])=>path==='/api/adb'&&options);
 assert.deepEqual(JSON.parse(command[1].body),{op:'wake',device:'phone'});
 resolveServer({error:'SSH unavailable'});await new Promise(setImmediate);
 assert.match(ids['tpmsg'].textContent,/SSH unavailable/);
 assert.equal(ids['tpf'].hidden,true);
 assert.equal(controls.find(b=>b.dataset.adb==='wake').disabled,false);
 tab('c');
 assert.equal(card.parentElement,ids['adb-home']);
 assert.equal(address.value,'127.0.0.1:40000');
 assert.equal(pair.value,'127.0.0.1:41000');assert.equal(ids['adb-code'].value,'123456');
 serverReply={url:'http://localhost:8095/?t=test'};await tabletLoad();
 assert.equal(ids['tpf'].hidden,false);assert.equal(ids['tpf'].src,serverReply.url);
 renderADB({...state,connected:false});
 assert.equal(controls.find(b=>b.dataset.adb==='wake').disabled,true);
 assert.equal(ids['adb-brightness-set'].disabled,true);
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run([node], input=script, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
