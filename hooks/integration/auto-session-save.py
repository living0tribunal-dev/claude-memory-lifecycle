#!/usr/bin/env python3
"""
Auto Session Save - PreCompact Hook
Exhaustive Session-Analyse und Backup VOR Auto-Compact

ZWECK:
- Session-Transcript automatisch analysieren
- Neue Befehle, Workflows, Learnings, Fehler extrahieren
- Strukturiert in claude-mem speichern
- Chroma DB + wichtige Files backupen
- RESUME_PROMPT.md generieren falls Projekt aktiv
- Context-Watchdog Counter resetten

AUFRUF: Von PreCompact Hook in settings.json
"""

import sys
import os
import json
import re
import time
from pathlib import Path
from datetime import datetime
import subprocess
import shutil

# Paths
HOME = Path(os.environ.get('HOME', os.path.expanduser('~')))
CLAUDE_DIR = HOME / '.claude'
TRANSCRIPTS_DIR = CLAUDE_DIR / 'projects'
CHROMA_DIR = HOME / '.claude-mem' / 'chroma'
BACKUPS_DIR = CLAUDE_DIR / 'backups' / 'auto-session-save'
CLAUDE_MEM_SCRIPT = HOME / '.claude' / 'scripts' / 'claude-mem.py'
MEMORY_BUFFER_SCRIPT = HOME / '.claude' / 'scripts' / 'memory-buffer.py'
WATCHDOG_STATE_FILE = CLAUDE_DIR / 'state' / 'context-watchdog.json'

# Logging
LOG_FILE = CLAUDE_DIR / 'hooks' / 'hook-debug.log'

def log(msg: str):
    """Write to hook debug log"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(f"auto-session-save: {timestamp} - {msg}\n")

def get_hook_input() -> dict:
    """Read hook input from stdin"""
    try:
        data = sys.stdin.read().strip()
        if data:
            return json.loads(data)
    except:
        pass
    return {}

def read_transcript(path: str) -> dict:
    """Read transcript from a specific path"""
    try:
        events = []
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        log(f"Loaded {len(events)} events from {path}")
        return {
            'path': path,
            'events': events,
            'last_modified': datetime.fromtimestamp(os.path.getmtime(path)).isoformat()
        }
    except Exception as e:
        log(f"ERROR reading transcript {path}: {e}")
        return None

def get_latest_transcript() -> dict:
    """Find and read latest transcript file (fallback)"""
    try:
        jsonl_files = list(TRANSCRIPTS_DIR.glob('**/*.jsonl'))
        if not jsonl_files:
            log("ERROR: No transcript files found")
            return None
        latest = max(jsonl_files, key=lambda p: p.stat().st_mtime)
        log(f"Found latest transcript: {latest}")
        return read_transcript(str(latest))
    except Exception as e:
        log(f"ERROR getting transcript: {e}")
        return None

def extract_user_messages(events: list) -> list:
    """Extract all user messages from transcript.

    Transcript format: {"type": "user", "message": {"role": "user", "content": ...}}
    Content can be a string or array of content blocks.
    """
    user_messages = []
    for event in events:
        msg = event.get('message', {})
        if not isinstance(msg, dict):
            continue
        if msg.get('role') == 'user':
            content = msg.get('content', '')
            if isinstance(content, str):
                if content.strip():
                    user_messages.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'text':
                        text = block.get('text', '')
                        if text.strip():
                            user_messages.append(text)
                    elif isinstance(block, str) and block.strip():
                        user_messages.append(block)
    return user_messages

def extract_tool_calls(events: list) -> list:
    """Extract all tool calls from transcript.

    Transcript format: {"message": {"role": "assistant", "content": [{"type": "tool_use", ...}]}}
    """
    tool_calls = []
    for event in events:
        msg = event.get('message', {})
        if not isinstance(msg, dict):
            continue
        if msg.get('role') == 'assistant':
            content = msg.get('content', [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'tool_use':
                        tool_calls.append({
                            'name': block.get('name'),
                            'input': block.get('input', {})
                        })
    return tool_calls

def extract_files_created(tool_calls: list) -> list:
    """Extract all files created during session"""
    files = set()
    for call in tool_calls:
        if call['name'] in ['Write', 'Edit']:
            file_path = call['input'].get('file_path')
            if file_path:
                files.add(file_path)
    return sorted(list(files))

def extract_commands_run(tool_calls: list) -> list:
    """Extract all bash commands run during session"""
    commands = []
    for call in tool_calls:
        if call['name'] == 'Bash':
            cmd = call['input'].get('command')
            desc = call['input'].get('description', '')
            if cmd:
                commands.append({'command': cmd, 'description': desc})
    return commands

def analyze_session(transcript: dict) -> dict:
    """Analyze session and extract key information"""
    events = transcript['events']

    user_messages = extract_user_messages(events)
    tool_calls = extract_tool_calls(events)

    # Determine main task from first substantial user message
    main_task = "Unknown task"
    for msg in user_messages:
        # Skip system/hook/command messages
        if not msg.startswith('<') and not msg.startswith('#') and len(msg) > 10:
            main_task = msg[:200] + "..." if len(msg) > 200 else msg
            break

    # Extract specifics
    files_created = extract_files_created(tool_calls)
    commands_run = extract_commands_run(tool_calls)

    # Detect patterns
    new_skills = []
    new_workflows = []

    for file_path in files_created:
        if '/.claude/commands/' in file_path or '\\.claude\\commands\\' in file_path:
            skill_name = Path(file_path).stem
            new_skills.append(skill_name)
        elif '/.claude/hooks/' in file_path or '\\.claude\\hooks\\' in file_path:
            hook_name = Path(file_path).stem
            new_workflows.append(f"Hook: {hook_name}")

    # Count tool usage
    tool_usage = {}
    for call in tool_calls:
        tool_name = call['name']
        tool_usage[tool_name] = tool_usage.get(tool_name, 0) + 1

    # Last user messages for RESUME_PROMPT context
    last_messages = []
    for msg in user_messages[-5:]:
        truncated = msg[:200] + "..." if len(msg) > 200 else msg
        if not truncated.startswith('<') and len(truncated.strip()) > 10:
            last_messages.append(truncated)

    return {
        'main_task': main_task,
        'user_message_count': len(user_messages),
        'tool_call_count': len(tool_calls),
        'files_created': files_created,
        'files_created_count': len(files_created),
        'commands_run': commands_run[:10],
        'commands_count': len(commands_run),
        'new_skills': new_skills,
        'new_workflows': new_workflows,
        'tool_usage': dict(sorted(tool_usage.items(), key=lambda x: x[1], reverse=True)[:10]),
        'last_modified': transcript['last_modified'],
        'last_user_messages': last_messages,
    }

def save_to_claude_mem(analysis: dict, date_tag: str):
    """Save analysis to claude-mem"""
    try:
        # Build structured summary
        summary = f"""AUTO-SESSION-SAVE {date_tag}

MAIN TASK: {analysis['main_task']}

SESSION STATISTICS:
- User Messages: {analysis['user_message_count']}
- Tool Calls: {analysis['tool_call_count']}
- Files Created: {analysis['files_created_count']}
- Commands Run: {analysis['commands_count']}

NEW SKILLS/COMMANDS:
{chr(10).join('- ' + s for s in analysis['new_skills']) if analysis['new_skills'] else '- None'}

NEW WORKFLOWS/HOOKS:
{chr(10).join('- ' + w for w in analysis['new_workflows']) if analysis['new_workflows'] else '- None'}

FILES CREATED:
{chr(10).join('- ' + f for f in analysis['files_created'][:20]) if analysis['files_created'] else '- None'}
{f"... and {len(analysis['files_created']) - 20} more" if len(analysis['files_created']) > 20 else ''}

TOP COMMANDS RUN:
{chr(10).join('- ' + c['command'][:100] for c in analysis['commands_run'][:5]) if analysis['commands_run'] else '- None'}

TOOL USAGE:
{chr(10).join(f"- {tool}: {count}x" for tool, count in list(analysis['tool_usage'].items())[:10])}

Last Modified: {analysis['last_modified']}
"""

        # Save via claude-mem.py
        result = subprocess.run(
            ['python', str(CLAUDE_MEM_SCRIPT), 'add', summary],
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode == 0:
            log(f"Saved to claude-mem: {len(summary)} chars")
        else:
            log(f"ERROR saving to claude-mem: {result.stderr}")

        # Dual-Write: auch in memory-buffer speichern
        try:
            buf_cmd = ['python', str(MEMORY_BUFFER_SCRIPT), 'add']
            cwd = os.getcwd()
            research_marker = '_RESEARCH' + os.sep
            if research_marker in cwd:
                parts = cwd.split(research_marker, 1)[1].split(os.sep)
                if parts and parts[0]:
                    buf_cmd.extend(['--project', parts[0]])
            buf_cmd.append(summary)
            buf_result = subprocess.run(
                buf_cmd,
                capture_output=True,
                text=True,
                timeout=10
            )
            if buf_result.returncode == 0:
                log(f"Saved to memory-buffer: {buf_result.stdout.strip()}")
            else:
                log(f"WARN memory-buffer: {buf_result.stderr.strip()}")
        except Exception as buf_e:
            log(f"WARN memory-buffer failed: {buf_e}")

        return result.returncode == 0

    except Exception as e:
        log(f"ERROR in save_to_claude_mem: {e}")
        return False

def backup_chroma_db(date_tag: str):
    """Backup Chroma vector database"""
    try:
        if not CHROMA_DIR.exists():
            log("Chroma directory not found, skipping backup")
            return False

        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        backup_dest = BACKUPS_DIR / f"chroma-{date_tag}"
        if backup_dest.exists():
            shutil.rmtree(backup_dest)
        shutil.copytree(CHROMA_DIR, backup_dest)
        size_mb = sum(f.stat().st_size for f in backup_dest.rglob('*') if f.is_file()) / (1024 * 1024)
        log(f"Chroma DB backed up: {backup_dest} ({size_mb:.2f} MB)")
        return True

    except Exception as e:
        log(f"ERROR backing up Chroma: {e}")
        return False

def backup_key_files(analysis: dict, date_tag: str):
    """Backup key files modified in session"""
    try:
        files_dir = BACKUPS_DIR / f"files-{date_tag}"
        files_dir.mkdir(parents=True, exist_ok=True)

        backed_up = 0
        for file_path in analysis['files_created'][:50]:
            try:
                src = Path(file_path)
                if src.exists() and src.is_file():
                    rel_path = src.relative_to(src.anchor) if src.is_absolute() else src
                    dest = files_dir / rel_path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    backed_up += 1
            except Exception as e:
                log(f"WARN: Could not backup {file_path}: {e}")
                continue

        if backed_up > 0:
            log(f"Backed up {backed_up} files to {files_dir}")
            return True
        else:
            log("No files backed up")
            return False

    except Exception as e:
        log(f"ERROR backing up files: {e}")
        return False

def save_analysis_json(analysis: dict, date_tag: str):
    """Save raw analysis as JSON for programmatic access"""
    try:
        json_file = BACKUPS_DIR / f"analysis-{date_tag}.json"
        json_file.parent.mkdir(parents=True, exist_ok=True)

        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(analysis, f, indent=2, ensure_ascii=False)

        log(f"Analysis JSON saved: {json_file}")
        return True

    except Exception as e:
        log(f"ERROR saving analysis JSON: {e}")
        return False

def detect_project(cwd: str) -> tuple:
    """Detect project name and path from CWD"""
    normalized = cwd.replace('\\', '/')
    if '_RESEARCH/' in normalized:
        parts = normalized.split('_RESEARCH/', 1)
        if len(parts) > 1:
            project_name = parts[1].split('/')[0]
            if project_name:
                idx = normalized.find('_RESEARCH/')
                project_path = normalized[:idx] + '_RESEARCH/' + project_name
                # Convert back to OS path
                project_path = project_path.replace('/', os.sep)
                return project_name, project_path
    return None, None

def generate_resume_prompt(analysis: dict, cwd: str, date_tag: str) -> bool:
    """Generate a basic RESUME_PROMPT.md for the active project (safety net)"""
    project_name, project_path = detect_project(cwd)
    if not project_name or not project_path:
        log("No project detected, skipping RESUME_PROMPT")
        return False

    if not os.path.isdir(project_path):
        log(f"Project path not found: {project_path}")
        return False

    resume_path = os.path.join(project_path, 'RESUME_PROMPT.md')

    # Don't overwrite if recently updated (within last 30 min = /session-save already ran)
    if os.path.exists(resume_path):
        age_minutes = (time.time() - os.path.getmtime(resume_path)) / 60
        if age_minutes < 30:
            log(f"RESUME_PROMPT.md is fresh ({age_minutes:.0f}min old), skipping")
            return False

    # Build content
    files_list = '\n'.join(f'- {f}' for f in analysis['files_created'][:15])
    if not files_list:
        files_list = '- Keine erkannt'

    last_context = '\n'.join(f'- {m}' for m in analysis.get('last_user_messages', []))
    if not last_context:
        last_context = '- Nicht extrahierbar'

    content = f"""# Resume Prompt -- {project_name} (AUTO-GENERATED vor Compact)
## Generiert: {datetime.now().strftime('%Y-%m-%d %H:%M')}
## Aktive Phase: siehe STATE.md
## Session: auto-compact save

## Letzter Stand
Auto-generiert vor Context-Compaction. Session hatte {analysis['user_message_count']} User-Messages und {analysis['tool_call_count']} Tool-Calls.

Hauptaufgabe: {analysis['main_task']}

## Letzte User-Aktivitaet
{last_context}

## Geaenderte Dateien
{files_list}

## Offene Punkte
Automatisch vor auto-compact generiert. Fuer vollstaendigen Kontext: /project-load oder claude-mem search.
"""

    try:
        with open(resume_path, 'w', encoding='utf-8') as f:
            f.write(content)
        log(f"RESUME_PROMPT.md written: {resume_path}")
        return True
    except Exception as e:
        log(f"ERROR writing RESUME_PROMPT: {e}")
        return False

def reset_watchdog_counter():
    """Reset the context watchdog counter after compaction"""
    try:
        state = {}
        if WATCHDOG_STATE_FILE.exists():
            with open(WATCHDOG_STATE_FILE, 'r') as f:
                state = json.load(f)
        state['message_count'] = 0
        state['compact_count'] = state.get('compact_count', 0) + 1
        state['last_time'] = time.time()
        WATCHDOG_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(WATCHDOG_STATE_FILE, 'w') as f:
            json.dump(state, f)
        log(f"Watchdog counter reset (compact #{state['compact_count']})")
    except Exception as e:
        log(f"WARN: Could not reset watchdog: {e}")

def cleanup_old_backups(keep_last_n: int = 10):
    """Clean up old backups, keep only last N"""
    try:
        if not BACKUPS_DIR.exists():
            return

        backup_sets = {}
        for item in BACKUPS_DIR.iterdir():
            match = re.search(r'(\d{8}-\d{6})', item.name)
            if match:
                date_tag = match.group(1)
                if date_tag not in backup_sets:
                    backup_sets[date_tag] = []
                backup_sets[date_tag].append(item)

        sorted_tags = sorted(backup_sets.keys(), reverse=True)
        for old_tag in sorted_tags[keep_last_n:]:
            for item in backup_sets[old_tag]:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            log(f"Cleaned up old backup: {old_tag}")

    except Exception as e:
        log(f"ERROR cleaning up backups: {e}")

def main():
    """Main execution"""
    log("=" * 60)
    log("AUTO-SESSION-SAVE START")
    log("=" * 60)

    date_tag = datetime.now().strftime('%Y%m%d-%H%M%S')

    # Read hook input from stdin
    hook_input = get_hook_input()
    transcript_path = hook_input.get('transcript_path')
    cwd = hook_input.get('cwd', os.getcwd())

    # 1. Get transcript
    log("Step 1: Getting transcript...")
    transcript = None
    if transcript_path and os.path.exists(transcript_path):
        log(f"Using transcript from hook input: {transcript_path}")
        transcript = read_transcript(transcript_path)
    if not transcript:
        log("Falling back to globbing for transcript...")
        transcript = get_latest_transcript()
    if not transcript:
        log("ERROR: Could not get transcript, aborting")
        return 1

    # 2. Analyze session
    log("Step 2: Analyzing session...")
    analysis = analyze_session(transcript)
    log(f"Analysis complete: {analysis['files_created_count']} files, {analysis['tool_call_count']} tool calls, {analysis['user_message_count']} user messages")

    # 3. Save to claude-mem
    log("Step 3: Saving to claude-mem...")
    mem_success = save_to_claude_mem(analysis, date_tag)

    # 4. Backup Chroma DB
    log("Step 4: Backing up Chroma database...")
    chroma_success = backup_chroma_db(date_tag)

    # 5. Backup key files
    log("Step 5: Backing up modified files...")
    files_success = backup_key_files(analysis, date_tag)

    # 6. Save analysis JSON
    log("Step 6: Saving analysis JSON...")
    json_success = save_analysis_json(analysis, date_tag)

    # 7. Generate RESUME_PROMPT.md
    log("Step 7: Generating RESUME_PROMPT.md...")
    resume_success = generate_resume_prompt(analysis, cwd, date_tag)

    # 8. Reset watchdog counter
    log("Step 8: Resetting watchdog counter...")
    reset_watchdog_counter()

    # 9. Cleanup old backups
    log("Step 9: Cleaning up old backups...")
    cleanup_old_backups(keep_last_n=10)

    # Summary
    log("=" * 60)
    log(f"AUTO-SESSION-SAVE COMPLETE - {date_tag}")
    log(f"  claude-mem: {'OK' if mem_success else 'FAIL'}")
    log(f"  Chroma backup: {'OK' if chroma_success else 'SKIP'}")
    log(f"  Files backup: {'OK' if files_success else 'SKIP'}")
    log(f"  Analysis JSON: {'OK' if json_success else 'FAIL'}")
    log(f"  RESUME_PROMPT: {'OK' if resume_success else 'SKIP'}")
    log("=" * 60)

    # Output for Claude Code (hook response)
    output = {
        "hookSpecificOutput": {
            "additionalContext": f"AUTO-SESSION-SAVE ({date_tag}): {analysis['files_created_count']} files, {analysis['tool_call_count']} tools, {analysis['user_message_count']} messages gesichert.{' RESUME_PROMPT.md generiert.' if resume_success else ''}"
        }
    }
    print(json.dumps(output))
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        log(f"FATAL ERROR: {e}")
        import traceback
        log(traceback.format_exc())
        sys.exit(1)
