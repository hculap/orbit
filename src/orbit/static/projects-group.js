'use strict';
// Pure grouping logic for the Projects view — group projects by Area.
//
// JSX-free + dependency-free so Node's built-in `node --test` can require() it
// directly (this no-build CDN-React project has no jsdom/bundler). The .jsx
// view is thin rendering on top of this function.
//
// `window` is referenced ONLY inside the publish guard at the very bottom, never
// at module load, so `require()` under Node never touches a missing global.
//
// Immutability: the function only READS its inputs and returns fresh
// arrays/objects — `projects`/`areas` are never mutated.

// groupProjectsByArea(projects, areas, filterText) → ordered group list.
//
// Each project appears under EVERY area in its `linked_areas`; projects with no
// linked_areas (or only stale links to areas absent from `areas`) go to the
// synthetic "__none__" bucket, always rendered last. Group order follows the
// `areas` order; empty groups are dropped.
function groupProjectsByArea(projects, areas, filterText) {
  const f = (filterText || '').trim().toLowerCase();
  const match = (p) => !f || (p.label || p.name || '').toLowerCase().includes(f);
  const filtered = (projects || []).filter(match);

  const areaOrder = (areas || []).map((a) => a.name);
  const areaMeta = {};
  for (const a of (areas || [])) areaMeta[a.name] = a;

  const buckets = {};
  const noArea = [];
  for (const p of filtered) {
    const links = (p.linked_areas || []).filter((a) => areaMeta[a]); // ignore stale links to unknown areas
    if (links.length === 0) {
      noArea.push(p);
      continue;
    }
    for (const a of links) {
      (buckets[a] = buckets[a] || []).push(p);
    }
  }

  const groups = [];
  for (const name of areaOrder) {
    if (buckets[name] && buckets[name].length) {
      const a = areaMeta[name] || {};
      groups.push({
        key: name,
        label: a.label || name,
        icon: a.icon || '📂',
        subtitle: '~/Areas/' + name + '/',
        projects: buckets[name],
      });
    }
  }
  if (noArea.length) {
    groups.push({
      key: '__none__',
      label: 'Bez area',
      icon: '🗂️',
      subtitle: 'projekty bez przypisanej area',
      projects: noArea,
    });
  }
  return groups;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { groupProjectsByArea };
}
if (typeof window !== 'undefined') {
  window.HubProjectsGroup = { groupProjectsByArea };
}
