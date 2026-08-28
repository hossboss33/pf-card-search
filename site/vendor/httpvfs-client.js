/* Minimal main-thread client for sql.js-httpvfs's sqlite.worker.js.
 *
 * Upstream ships two halves: the worker (vendored here as sqlite.worker.js,
 * MIT, github.com/phiresky/sql.js-httpvfs) and an ESM entry point that wraps
 * the worker with Comlink. Only the worker and sql-wasm.wasm were vendored, and
 * the entry point needs a bundler, so the small piece of Comlink wire protocol
 * the worker actually speaks is reimplemented here against the same message
 * shapes (Comlink, Apache-2.0, github.com/GoogleChromeLabs/comlink).
 *
 * No build step, no CDN: this file is plain ES5-ish script.
 *
 * The worker exposes:
 *   SplitFileHttpDatabase(wasmUrl, configs, virtualFilename?, maxBytesToRead?)
 *     -> a proxy port for a sql.js Database that also carries .query(sql, params)
 *   getStats(), getResetAccessedPages()
 */
(function (global) {
  "use strict";

  var APPLY = 2;
  var RAW = 0;
  var HANDLER = 3;

  function uid() {
    var out = [];
    for (var i = 0; i < 4; i++) {
      out.push(Math.floor(Math.random() * Number.MAX_SAFE_INTEGER).toString(16));
    }
    return out.join("-");
  }

  // Mirror of Comlink's fromWireValue for the handlers this worker uses.
  function fromWire(wire) {
    if (!wire) return wire;
    if (wire.type === HANDLER) {
      if (wire.name === "throw") {
        var v = wire.value;
        if (v && v.isError) {
          var err = new Error(v.value.message);
          err.name = v.value.name;
          err.stack = v.value.stack;
          throw err;
        }
        throw v ? v.value : new Error("worker threw");
      }
      if (wire.name === "WORKERSQLPROXIES" || wire.name === "proxy") {
        var port = wire.value;
        port.start();
        return port;
      }
      throw new Error("httpvfs: unsupported transfer handler " + wire.name);
    }
    return wire.value;
  }

  function request(endpoint, msg) {
    return new Promise(function (resolve, reject) {
      var id = uid();
      function onMessage(ev) {
        if (!ev.data || ev.data.id !== id) return;
        endpoint.removeEventListener("message", onMessage);
        try {
          resolve(fromWire(ev.data));
        } catch (err) {
          reject(err);
        }
      }
      endpoint.addEventListener("message", onMessage);
      if (endpoint.start) endpoint.start();
      msg.id = id;
      endpoint.postMessage(msg);
    });
  }

  function call(endpoint, path, args) {
    return request(endpoint, {
      type: APPLY,
      path: path,
      argumentList: args.map(function (a) {
        return { type: RAW, value: a };
      })
    });
  }

  /* configs: [{ from: "inline", config: { serverMode: "full",
   *             url: "db/cards.sqlite", requestChunkSize: 1024 } }] */
  function createDbWorker(configs, workerUrl, wasmUrl) {
    var worker = new Worker(workerUrl);
    var fatal = null;
    worker.addEventListener("error", function (ev) {
      fatal = ev.message || "worker error";
    });
    return call(worker, ["SplitFileHttpDatabase"], [wasmUrl, configs]).then(
      function (port) {
        return {
          worker: worker,
          port: port,
          // rows as objects
          query: function (sql, params) {
            return call(port, ["query"], [sql, params || []]);
          },
          // raw sql.js [{columns, values}]
          exec: function (sql, params) {
            return call(port, ["exec"], [sql, params || []]);
          },
          stats: function () {
            return call(worker, ["getStats"], []);
          },
          close: function () {
            worker.terminate();
          }
        };
      },
      function (err) {
        throw new Error(fatal || err.message || String(err));
      }
    );
  }

  global.httpvfs = { createDbWorker: createDbWorker };
})(self);
