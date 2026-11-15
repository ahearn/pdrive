// Preload (node --require): resolve hostnames via Cloudflare instead of the system resolver,
// for when the local router's DNS stops answering. Only affects this Node process.
const dns = require('dns');
const resolver = new dns.Resolver({ timeout: 3000, tries: 3 });
resolver.setServers(['1.1.1.1', '1.0.0.1']);
const origLookup = dns.lookup;
dns.lookup = function (hostname, options, callback) {
  if (typeof options === 'function') { callback = options; options = {}; }
  if (typeof options === 'number') options = { family: options };
  options = options || {};
  if (options.family === 6 || hostname === 'localhost' || /^[\d.]+$/.test(hostname) || hostname.includes(':')) {
    return origLookup.call(dns, hostname, options, callback);
  }
  resolver.resolve4(hostname, (err, addrs) => {
    if (err || !addrs.length) return origLookup.call(dns, hostname, options, callback);
    if (options.all) return callback(null, addrs.map(a => ({ address: a, family: 4 })));
    callback(null, addrs[0], 4);
  });
};
