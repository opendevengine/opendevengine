// Minimal CSS cascade for the DOM stand-in: computes the declared values that
// apply to one element from opendevengine/web/styles.css, honoring selector matching,
// specificity, source order, !important and the :focus/:focus-visible state.
//
// Limits: top-level rules only (desktop width; @media blocks are skipped), no
// inheritance, no var() resolution, no layout. Selectors with other
// pseudo-classes or pseudo-elements are treated as not applying. It answers
// "which declarations win for this element", not "what pixels are painted".

import { matches } from "./dom.mjs";

export function parseRules(css) {
  const text = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const rules = [];
  let position = 0;
  while (position < text.length) {
    const open = text.indexOf("{", position);
    if (open === -1) break;
    const prelude = text.slice(position, open).trim();
    let depth = 1;
    let close = open + 1;
    for (; close < text.length && depth; close += 1) {
      if (text[close] === "{") depth += 1;
      else if (text[close] === "}") depth -= 1;
    }
    const body = text.slice(open + 1, close - 1);
    position = close;
    if (prelude.startsWith("@")) continue;
    const declarations = body.split(";").map((item) => item.trim()).filter(Boolean).map((item) => {
      const colon = item.indexOf(":");
      const property = item.slice(0, colon).trim();
      let value = item.slice(colon + 1).trim();
      const important = /!important$/.test(value);
      if (important) value = value.replace(/\s*!important$/, "");
      return { property, value, important };
    });
    for (const selector of prelude.split(",").map((item) => item.trim())) {
      rules.push({ selector, declarations, order: rules.length });
    }
  }
  return rules;
}

function specificity(selector) {
  const plain = selector.replace(/\[[^\]]*\]/g, "[]");
  const ids = (plain.match(/#[\w-]+/g) || []).length;
  const classes = (plain.match(/\.[\w-]+|\[\]|:(?!:)[\w-]+/g) || []).length;
  const tags = plain.split(/[\s>+~]+/).filter((compound) => /^[a-zA-Z]/.test(compound)).length;
  return ids * 10000 + classes * 100 + tags;
}

function applies(selector, element, focused) {
  if (selector.includes("::")) return false;
  const focusMatch = /:focus(?:-visible)?$/.exec(selector);
  const base = focusMatch ? selector.slice(0, focusMatch.index) : selector;
  if (base.includes(":") || base === "") return false;
  if (focusMatch && !focused) return false;
  try {
    return matches(element, base);
  } catch {
    return false;
  }
}

export function declaredStyle(element, css, { focused = false } = {}) {
  const winners = [];
  for (const rule of parseRules(css)) {
    if (!applies(rule.selector, element, focused)) continue;
    const weight = specificity(rule.selector);
    for (const declaration of rule.declarations) winners.push({ ...declaration, weight, order: rule.order });
  }
  winners.sort((a, b) => (a.important - b.important) || (a.weight - b.weight) || (a.order - b.order));
  const style = {};
  for (const { property, value } of winners) style[property] = value;
  return style;
}
