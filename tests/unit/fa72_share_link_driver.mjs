// Implements: FA72 - runs the pure helpers of frontend/lib/shareLink.ts under
// node (type stripping) and prints the results as JSON for the pytest module.
import { pathToFileURL } from "node:url";

const mod = await import(pathToFileURL(process.argv[2]).href);
const { parseScenarioParam, buildScenarioSearch, withScenarioParam, urlWithScenario, planLinkOpen, linkErrorAction } = mod;

const refs = [
  { source: "example", id: "leipzig/leipzig10_erreichbarkeit" },
  { source: "example", id: "a:b/c" },
  { source: "user", id: "lonely-villages" },
  { source: "user", id: "Müller & Söhne+Co 100% ü" },
  { source: "user", id: "a b" },
  { source: "user", id: "x:y:z" },
];
const out = {};
out.parse = Object.fromEntries(
  ["?scenario=example:", "?scenario=foo:bar", "?scenario=example", "?scenario=", "?other=1", "",
   "?scenario=user:a:b", "?scenario=example:a/b"].map((q) => [q, parseScenarioParam(q)])
);
out.roundtrip = refs.map((r) => ({ ref: r, search: buildScenarioSearch(r), parsed: parseScenarioParam(buildScenarioSearch(r)) }));
out.keep_others = {
  set: withScenarioParam("?a=1&b=x%20y", refs[0]),
  replace: withScenarioParam("?scenario=user:old&a=1", refs[2]),
  remove: withScenarioParam("?a=1&scenario=user:old&b=2", null),
  remove_only: withScenarioParam("?scenario=user:old", null),
  empty: withScenarioParam("", null),
};
out.url = {
  set: urlWithScenario({ pathname: "/app", search: "?a=1", hash: "#sec" }, refs[2]),
  remove: urlWithScenario({ pathname: "/app", search: "?scenario=user:x&a=1", hash: "#sec" }, null),
  remove_plain: urlWithScenario({ pathname: "/", search: "?scenario=user:x", hash: "" }, null),
};
const ok = (r) => ({ kind: "ok", ref: r });
out.plan = {
  none: planLinkOpen({ kind: "none" }, "example:x"),
  invalid: planLinkOpen({ kind: "invalid", raw: "foo" }, null),
  open_no_marker: planLinkOpen(ok(refs[2]), null),
  open_other_marker: planLinkOpen(ok(refs[2]), "user:other"),
  restore: planLinkOpen(ok(refs[2]), "user:lonely-villages"),
};
out.error_action = { notfound: linkErrorAction(true), other: linkErrorAction(false) };
console.log(JSON.stringify(out));
