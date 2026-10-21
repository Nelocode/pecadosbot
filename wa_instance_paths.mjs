/** Keep this checkout's worker away from another brand's files. */
import fs from 'node:fs';
import path from 'node:path';

function resolvedExistingAncestor(candidate) {
  const missing = [];
  let current = candidate;
  while (true) {
    try {
      fs.lstatSync(current);
      break;
    } catch (error) {
      if (error?.code !== 'ENOENT') throw new Error('instance_path_unavailable');
    }
    const parent = path.dirname(current);
    if (parent === current) throw new Error('instance_path_unavailable');
    missing.unshift(path.basename(current));
    current = parent;
  }
  try { return path.join(fs.realpathSync(current), ...missing); }
  catch { throw new Error('instance_path_unavailable'); }
}

function contained(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative === '' || (!relative.startsWith(`..${path.sep}`)
    && relative !== '..' && !path.isAbsolute(relative));
}

function validateDataLinks(root, directory = root) {
  if (!fs.existsSync(directory)) return;
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const candidate = path.join(directory, entry.name);
    if (entry.isSymbolicLink()) {
      if (!contained(root, fs.realpathSync(candidate))) throw new Error('instance_path_mismatch');
    } else if (entry.isDirectory()) {
      validateDataLinks(root, candidate);
    }
  }
}

export function resolveWhatsAppInstancePaths(moduleDirectory, environ = process.env) {
  const baseDir = fs.realpathSync(moduleDirectory);
  if (environ.BOT_DIR && resolvedExistingAncestor(path.resolve(environ.BOT_DIR)) !== baseDir) {
    throw new Error('instance_root_mismatch');
  }
  const dataDir = path.join(baseDir, 'data');
  // Bind mounts retain their local path. Symlinks to a different checkout do not.
  if (resolvedExistingAncestor(dataDir) !== dataDir) {
    throw new Error('instance_data_redirected');
  }
  // Inspect directory entries only; never read credentials or follow links.
  validateDataLinks(dataDir);
  function own(name, fallback, root = dataDir) {
    const candidate = environ[name] ? path.resolve(baseDir, environ[name]) : fallback;
    if (!contained(root, candidate) || !contained(root, resolvedExistingAncestor(candidate))) {
      throw new Error('instance_path_mismatch');
    }
    return candidate;
  }
  const officialImage = path.join(baseDir, 'assets', 'pecadosvip-profile-logo.png');
  return {
    baseDir,
    dataDir,
    audioDir: own('', path.join(dataDir, 'audios')),
    videoDir: own('', path.join(dataDir, 'videos')),
    messagesFile: own('', path.join(dataDir, 'messages.json')),
    authDir: own('WA_AUTH_DIR', path.join(dataDir, 'wa_auth')),
    qrPath: own('WA_QR_PATH', path.join(baseDir, 'wa_qr.png'), baseDir),
    callHealthFile: own('WA_HEALTH_FILE', path.join(dataDir, 'wa_call_health.json')),
    safetyHealthFile: own('WA_SAFETY_HEALTH_FILE', path.join(dataDir, 'wa_safety_health.json')),
    safetyControlFile: own('WA_SAFETY_CONTROL_FILE', path.join(dataDir, 'wa_safety_control.json')),
    interactionStateFile: own('WA_INTERACTION_STATE_FILE', path.join(dataDir, 'wa_interaction_state.json')),
    voiceNoteCacheDir: own('WA_VOICE_NOTE_CACHE_DIR', path.join(dataDir, 'wa_voice_notes')),
    officialImage,
    profileImage: own('WA_PROFILE_PICTURE_PATH', officialImage, baseDir),
    profileStateFile: own('WA_PROFILE_PICTURE_STATE_FILE', path.join(dataDir, 'wa_profile_picture_state.json')),
    profileRequestFile: own('', path.join(dataDir, 'wa_profile_picture_request.json')),
    identityFile: own('WA_IDENTITY_FILE', path.join(dataDir, 'wa_identity.json')),
  };
}
