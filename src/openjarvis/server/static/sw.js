/**
 * Copyright 2018 Google Inc. All Rights Reserved.
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *     http://www.apache.org/licenses/LICENSE-2.0
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

// If the loader is already loaded, just stop.
if (!self.define) {
  let registry = {};

  // Used for `eval` and `importScripts` where we can't get script URL by other means.
  // In both cases, it's safe to use a global var because those functions are synchronous.
  let nextDefineUri;

  const singleRequire = (uri, parentUri) => {
    uri = new URL(uri + ".js", parentUri).href;
    return registry[uri] || (
      
        new Promise(resolve => {
          if ("document" in self) {
            const script = document.createElement("script");
            script.src = uri;
            script.onload = resolve;
            document.head.appendChild(script);
          } else {
            nextDefineUri = uri;
            importScripts(uri);
            resolve();
          }
        })
      
      .then(() => {
        let promise = registry[uri];
        if (!promise) {
          throw new Error(`Module ${uri} didn’t register its module`);
        }
        return promise;
      })
    );
  };

  self.define = (depsNames, factory) => {
    const uri = nextDefineUri || ("document" in self ? document.currentScript.src : "") || location.href;
    if (registry[uri]) {
      // Module is already loading or loaded.
      return;
    }
    let exports = {};
    const require = depUri => singleRequire(depUri, uri);
    const specialDeps = {
      module: { uri },
      exports,
      require
    };
    registry[uri] = Promise.all(depsNames.map(
      depName => specialDeps[depName] || require(depName)
    )).then(deps => {
      factory(...deps);
      return exports;
    });
  };
}
define(['./workbox-7e5eb42b'], (function (workbox) { 'use strict';

  self.skipWaiting();
  workbox.clientsClaim();
  /**
   * The precacheAndRoute() method efficiently caches and responds to
   * requests for URLs in the manifest.
   * See https://goo.gl/S9QRab
   */
  workbox.precacheAndRoute([{
    "url": "registerSW.js",
    "revision": "1872c500de691dce40960bb85481de07"
  }, {
    "url": "pwa-512x512.png",
    "revision": "1dbaf4282c5d869506e0be590c5138a4"
  }, {
    "url": "pwa-192x192.png",
    "revision": "effb107657d2554e559889eabdca8b94"
  }, {
    "url": "index.html",
    "revision": "c552afceeb8ee7765105f261d6b048fd"
  }, {
    "url": "favicon.ico",
    "revision": "113bd901f6ffd431dbe5de0e865c139c"
  }, {
    "url": "apple-touch-icon.png",
    "revision": "994f2cbe60c90395d3a6b7a3db88303e"
  }, {
    "url": "assets/router-CY5fsh9m.js",
    "revision": null
  }, {
    "url": "assets/react-C9oaej_u.js",
    "revision": null
  }, {
    "url": "assets/markdown-DQXrgs4m.js",
    "revision": null
  }, {
    "url": "assets/index-yW3ljqrP.js",
    "revision": null
  }, {
    "url": "assets/index-DqqfIfNL.js",
    "revision": null
  }, {
    "url": "assets/index-Dm_QtxGQ.js",
    "revision": null
  }, {
    "url": "assets/index-CAH3XQkg.js",
    "revision": null
  }, {
    "url": "assets/index-C5xe0R6h.css",
    "revision": null
  }, {
    "url": "assets/core-DhEqZVGG.js",
    "revision": null
  }, {
    "url": "assets/charts-CUDA9B_8.js",
    "revision": null
  }, {
    "url": "pwa-192x192.png",
    "revision": "effb107657d2554e559889eabdca8b94"
  }, {
    "url": "pwa-512x512.png",
    "revision": "1dbaf4282c5d869506e0be590c5138a4"
  }, {
    "url": "manifest.webmanifest",
    "revision": "f80f66024c6986025cdbccf00744fa28"
  }], {});
  workbox.cleanupOutdatedCaches();
  workbox.registerRoute(new workbox.NavigationRoute(workbox.createHandlerBoundToURL("index.html"), {
    denylist: [/^\/v1\//, /^\/health/, /^\/dashboard/, /^\/api\//]
  }));

}));
