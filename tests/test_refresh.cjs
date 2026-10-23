// Focused frontend regression check. Run with: node tests/test_refresh.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../public/index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
const refreshSource = script.slice(script.indexOf('async function refresh()'), script.indexOf('function demoBar()'));

function fixture() {
    const panel = {innerHTML: 'Existing rider card'};
    let resolve, reject, renders = 0;
    const assignment = new Promise((ok, fail) => { resolve = ok; reject = fail; });
    const errors = [];
    const context = vm.createContext({
        me: {user_id: 'rider-user', role: 'rider', entity_id: 1},
        view: 'rider', busy: false, refreshPending: false, map: null,
        offset: 0, selectedRider: null, selectedShelter: null,
        $: () => panel, e: String, queueMicrotask,
        api: async url => url === '/me' ? {now: new Date().toISOString()} : assignment,
        drawRider(data, id) {
            assert.equal(id, 1);
            assert.equal(data.rider.name, 'Updated R1');
            panel.innerHTML = 'Updated rider card';
            renders++;
        },
        tick() {}, toast: message => errors.push(message),
    });
    vm.runInContext(refreshSource, context);
    return {panel, context, resolve, reject, errors, renders: () => renders};
}

(async () => {
    const success = fixture();
    const refreshing = success.context.refresh();
    await new Promise(setImmediate);
    assert.equal(success.panel.innerHTML, 'Existing rider card', 'The current card must remain visible during the fetch');
    success.resolve({rider: {name: 'Updated R1'}, assignment: null});
    await refreshing;
    assert.equal(success.panel.innerHTML, 'Updated rider card');
    assert.equal(success.renders(), 1);

    const failure = fixture();
    const failedRefresh = failure.context.refresh();
    await new Promise(setImmediate);
    failure.reject(new Error('Network unavailable'));
    await failedRefresh;
    assert.equal(failure.panel.innerHTML, 'Existing rider card', 'A failed fetch must not blank the current card');
    assert.deepEqual(failure.errors, ['Network unavailable']);
    assert.equal(failure.renders(), 0);
    console.log('Refresh regression passed: old card stays mounted during loading and after network errors.');
})().catch(error => { console.error(error); process.exitCode = 1; });
