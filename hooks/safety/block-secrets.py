#!/usr/bin/env python3
"""
PreToolUse Security Hook - Blockiert Zugriff auf sensitive Dateien
Adaptiert von TheDecipherist/claude-code-mastery

Exit Codes:
  0 = Allow operation
  1 = Error (user notification only)
  2 = BLOCK operation + stderr sent to Claude (Claude learns why!)
"""

import sys
import json
import os

# ============================================================================
# KONFIGURATION: Sensitive Dateien
# ============================================================================

SENSITIVE_FILENAMES = {
    # Environment & Secrets
    '.env', '.env.local', '.env.development', '.env.production', '.env.test',
    'secrets.json', 'secrets.yaml', 'secrets.yml',
    '.secrets', 'credentials.json', 'credentials.yaml',

    # SSH & Keys
    'id_rsa', 'id_rsa.pub', 'id_ed25519', 'id_ed25519.pub',
    'id_dsa', 'id_ecdsa', 'known_hosts', 'authorized_keys',

    # Package Manager Auth
    '.npmrc', '.pypirc', '.gem/credentials',

    # Cloud Credentials
    '.aws/credentials', '.aws/config',
    'gcloud.json', 'service-account.json',
    '.azure/credentials',

    # Database
    '.pgpass', '.my.cnf', '.mongorc.js',

    # Docker
    '.docker/config.json',

    # Kubernetes
    'kubeconfig', '.kube/config',
}

SENSITIVE_EXTENSIONS = {
    '.pem', '.key', '.p12', '.pfx', '.jks', '.keystore',
    '.crt', '.cer', '.der',
}

SENSITIVE_PATTERNS = [
    'secret', 'credential', 'private_key', 'privatekey',
    'password', 'passwd', 'api_key', 'apikey',
    '/secrets/', '/.secrets/',
    'token', 'auth',
]

# Whitelist - diese Dateien sind OK
WHITELIST = {
    '.env.example', '.env.template', '.env.sample',
    'secrets.example.json', 'credentials.example.json',
}

# ============================================================================
# LOGIK
# ============================================================================

def is_sensitive_file(filepath: str) -> tuple[bool, str]:
    """
    Prüft ob eine Datei sensitiv ist.
    Returns: (is_sensitive, reason)
    """
    if not filepath:
        return False, ""

    # Normalisieren
    filepath_lower = filepath.lower().replace('\\', '/')
    filename = os.path.basename(filepath_lower)

    # Whitelist prüfen
    if filename in WHITELIST:
        return False, ""

    # Check 1: Exakte Dateinamen
    if filename in SENSITIVE_FILENAMES:
        return True, f"Dateiname '{filename}' ist in der Sensitive-Liste"

    # Check 2: Dateierweiterungen
    _, ext = os.path.splitext(filename)
    if ext in SENSITIVE_EXTENSIONS:
        return True, f"Dateierweiterung '{ext}' ist sensitiv (Schlüssel/Zertifikat)"

    # Check 3: Pfad-Muster
    for pattern in SENSITIVE_PATTERNS:
        if pattern in filepath_lower:
            return True, f"Pfad enthält sensitives Muster '{pattern}'"

    return False, ""


def _block_msg(*lines):
    """Build multi-line block message."""
    return chr(10).join(lines)


def main():
    """Hauptfunktion - liest stdin JSON und prüft Dateipfade."""
    from platform_adapter import HookContext
    ctx = HookContext("PreToolUse")

    filepath = None
    if ctx.tool_name in ('Read', 'Edit', 'Write'):
        filepath = ctx.tool_input.get('file_path', '')
    elif ctx.tool_name == 'Bash':
        command = ctx.tool_input.get('command', '')
        for sensitive in SENSITIVE_FILENAMES:
            if sensitive in command.lower():
                ctx.block(_block_msg(
                    f"BLOCKED: Bash-Kommando referenziert sensitive Datei '{sensitive}'",
                    "Grund: Diese Datei könnte Secrets enthalten.",
                    "Lösung: Verwende .env.example als Template oder frage den User."
                ))
        sys.exit(0)

    if not filepath:
        sys.exit(0)

    is_sensitive, reason = is_sensitive_file(filepath)

    if is_sensitive:
        ctx.block(_block_msg(
            f"BLOCKED: Zugriff auf '{filepath}' verweigert",
            f"Grund: {reason}",
            "Dies ist eine Sicherheitsmaßnahme um Secrets zu schützen.",
            "Lösung: Falls du den Inhalt wirklich brauchst, frage den User."
        ))

    sys.exit(0)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f"block-secrets.py Warnung: {e}", file=sys.stderr)
        sys.exit(0)
