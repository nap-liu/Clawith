// Runtime features are not lowered by Vite's build.target. Keep this list
// explicit so older WebViews only download the compatibility code we use.
import 'core-js/modules/es.array.at.js';
import 'core-js/modules/es.array.flat-map.js';
import 'core-js/modules/es.object.from-entries.js';
import 'core-js/modules/es.object.has-own.js';
import 'core-js/modules/es.promise.all-settled.js';
import 'core-js/modules/web.queue-microtask.js';
import 'core-js/modules/web.structured-clone.js';
import 'core-js/modules/web.url-search-params.js';
import 'abortcontroller-polyfill/dist/polyfill-patch-fetch';
