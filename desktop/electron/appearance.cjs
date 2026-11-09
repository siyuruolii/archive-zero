const fs = require('node:fs');
const path = require('node:path');
const defaults = { character: true, motion: true };

function validateAppearance(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).some(key => !Object.hasOwn(defaults, key)) ||
      Object.keys(defaults).some(key => typeof value[key] !== 'boolean')) throw new Error('无效的外观设置');
  return { character: value.character, motion: value.motion };
}

function readAppearance(directory) {
  try {
    const file = path.join(directory, 'appearance.json');
    if (fs.statSync(file).size > 8192) return { ...defaults };
    return validateAppearance(JSON.parse(fs.readFileSync(file, 'utf8')));
  } catch { return { ...defaults }; }
}

function writeAppearance(directory, value) {
  const settings = validateAppearance(value);
  fs.mkdirSync(directory, { recursive: true });
  const file = path.join(directory, 'appearance.json');
  fs.writeFileSync(file + '.tmp', JSON.stringify(settings) + '\n', { mode: 0o600 });
  fs.renameSync(file + '.tmp', file);
  return settings;
}
module.exports = { validateAppearance, readAppearance, writeAppearance };
