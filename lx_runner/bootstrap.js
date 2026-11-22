(() => {
  const native = globalThis.__native;
  delete globalThis.__native;
  const call = (op, args) => {
    const r = JSON.parse(native(JSON.stringify({op, args})));
    if (r.error) throw new Error(r.error);
    return r.value;
  };
  let handler, initialized, finished, result, failure, timers = new Map(), sequence = 0;
  const buffer = {
    from(value, encoding = 'utf8') { return call('buffer', [value, encoding]); },
    bufToString(value, encoding = 'utf8') { return call('string', [value, encoding]); },
  };
  const crypto = {};
  for (const name of ['md5', 'randomBytes', 'aesEncrypt', 'rsaEncrypt']) crypto[name] = (...args) => call(name, args);
  const scriptInfo = JSON.parse(globalThis.__scriptInfo);
  delete globalThis.__scriptInfo;
  globalThis.console = Object.fromEntries(['log','warn','error','info','debug','group','groupEnd','time','timeEnd'].map(k => [k, () => {}]));
  globalThis.setTimeout = (callback, delay = 0, ...args) => { const id = ++sequence; timers.set(id, {callback: () => callback(...args), at: Date.now() + Math.min(10000, Math.max(0, delay))}); return id; };
  globalThis.clearTimeout = id => timers.delete(id);
  globalThis.lx = Object.freeze({
    version: '2.0.0', env: 'desktop', currentScriptInfo: scriptInfo,
    EVENT_NAMES: Object.freeze({inited:'inited', request:'request', updateAlert:'updateAlert'}),
    on(name, fn) { if (name === 'request') handler = fn; },
    send(name, value) { if (name === 'inited') initialized = value; return Promise.resolve(); },
    request(url, options, callback) {
      let aborted = false;
      Promise.resolve().then(() => {
        if (aborted) return;
        let response;
        try { response = call('request', [url, options || {}]); }
        catch(e) { callback(e, null, null); return; }
        callback(null, response, response.body);
      });
      return () => { aborted = true; };
    },
    utils: Object.freeze({buffer, crypto, zlib: {
      inflate: value => Promise.resolve(call('inflate', [value])),
      deflate: value => Promise.resolve(call('deflate', [value])),
    }}),
  });
  globalThis.__lxState = () => JSON.stringify({initialized, finished, result, failure});
  globalThis.__lxTick = () => { const at = Date.now(); for (const [id, t] of timers) if (t.at <= at) { timers.delete(id); t.callback(); } };
  globalThis.__lxInvoke = raw => {
    if (typeof handler !== 'function') { failure = 'missing_handler'; finished = true; return; }
    Promise.resolve().then(() => handler(JSON.parse(raw))).then(value => { result = value; finished = true; }, () => { failure = 'script_rejected'; finished = true; });
  };
})();
