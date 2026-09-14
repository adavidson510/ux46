/* Optional modules do not imply accounts, remote connectivity or sharing. */
window.__ux46modules = {email: false, tell: false, constellation: true};
for (const id of ['btnEmail', 'btnTell']) document.getElementById(id).hidden = true;
fetch('/api/modules', {credentials: 'same-origin'}).then(r => r.ok ? r.json() : null).then(modules => {
  if (!modules) return;
  window.__ux46modules = modules;
  for (const [name, id] of Object.entries({email:'btnEmail', tell:'btnTell', constellation:'btnConstellation'})) {
    document.getElementById(id).hidden = !modules[name];
  }
}).catch(() => {});
