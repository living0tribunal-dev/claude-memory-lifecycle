#!/usr/bin/env python3
"""
Agent Results Persistence - PostToolUse Hook for Agent tool
Automatically saves agent findings to ARTIFACTS/ directory.

Fires after every Agent tool call. Saves the full result to disk
so it survives context compaction and session-save compression.
"""

import sys
import json
import os
from pathlib import Path
from datetime import datetime

LOG_FILE = Path(os.path.expanduser('~')) / '.claude' / 'hooks' / 'hook-debug.log'
MIN_RESULT_LENGTH = 200  # Only persist substantial results


def log(msg: str):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(f"agent-persist: {timestamp} - {msg}\n")


def detect_artifacts_dir(cwd: str) -> str:
    """Find ARTIFACTS dir: _RESEARCH/[project]/ARTIFACTS/ or CWD/ARTIFACTS/"""
    normalized = cwd.replace('\\', '/')
    if '_RESEARCH/' in normalized:
        parts = normalized.split('_RESEARCH/', 1)
        if len(parts) > 1:
            project_name = parts[1].split('/')[0]
            if project_name:
                idx = normalized.find('_RESEARCH/')
                project_path = normalized[:idx] + '_RESEARCH/' + project_name
                return os.path.join(project_path.replace('/', os.sep), 'ARTIFACTS')
    return os.path.join(cwd, 'ARTIFACTS')


def read_result_from_transcript(transcript_path: str, tool_use_id: str) -> str:
    """Fallback: read agent result from transcript JSONL by matching tool_use_id."""
    try:
        result_text = ''
        with open(transcript_path, 'r', encoding='utf-8') as f:
            for line in reversed(f.readlines()):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = event.get('message', {})
                if not isinstance(msg, dict):
                    continue
                # Look for tool_result in user messages (API format)
                content = msg.get('content', [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get('type') == 'tool_result':
                            if block.get('tool_use_id') == tool_use_id:
                                # Extract text from content
                                inner = block.get('content', '')
                                if isinstance(inner, str):
                                    return inner
                                elif isinstance(inner, list):
                                    parts = []
                                    for part in inner:
                                        if isinstance(part, dict) and part.get('type') == 'text':
                                            parts.append(part.get('text', ''))
                                    return '\n'.join(parts)
                # Also check: assistant message that references this tool_use_id
                if msg.get('role') == 'assistant' and isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get('type') == 'text':
                            # If we find the tool_use_id nearby, this might contain the result
                            pass  # text blocks don't reference tool_use_id directly
        return result_text
    except Exception as e:
        log(f"Transcript fallback error: {e}")
        return ''


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except Exception:
        return

    tool_name = input_data.get('tool_name', '')
    if tool_name != 'Agent':
        return

    # Get result
    tool_result = input_data.get('tool_result', '')
    if isinstance(tool_result, dict):
        tool_result = json.dumps(tool_result, ensure_ascii=False, indent=2)
    tool_result = str(tool_result)

    if len(tool_result) < MIN_RESULT_LENGTH:
        log(f"Agent result too short ({len(tool_result)} chars). Input keys: {list(input_data.keys())}")
        # Fallback: read from transcript
        transcript_path = input_data.get('transcript_path', '')
        tool_use_id = input_data.get('tool_use_id', '')
        if transcript_path and tool_use_id:
            tool_result = read_result_from_transcript(transcript_path, tool_use_id)
            if len(tool_result) < MIN_RESULT_LENGTH:
                log(f"Transcript fallback also short ({len(tool_result)} chars), skipping")
                return
            log(f"Got result from transcript fallback ({len(tool_result)} chars)")
        else:
            log(f"No transcript_path or tool_use_id for fallback, skipping")
            return

    # Get prompt for context
    tool_input = input_data.get('tool_input', {})
    prompt = tool_input.get('prompt', 'No prompt available')
    prompt_summary = prompt[:500] + '...' if len(prompt) > 500 else prompt

    # Detect output directory
    cwd = input_data.get('cwd', os.getcwd())
    artifacts_dir = detect_artifacts_dir(cwd)
    os.makedirs(artifacts_dir, exist_ok=True)

    # Generate filename
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    filename = f"agent-{timestamp}.md"
    filepath = os.path.join(artifacts_dir, filename)

    # Avoid collisions (multiple agents in same second)
    counter = 1
    while os.path.exists(filepath):
        filename = f"agent-{timestamp}-{counter}.md"
        filepath = os.path.join(artifacts_dir, filename)
        counter += 1

    # Write file
    content = f"""# Agent Result -- {datetime.now().strftime('%Y-%m-%d %H:%M')}

## Prompt (truncated)
{prompt_summary}

## Result
{tool_result}
"""

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)

    log(f"Saved: {filepath} ({len(tool_result)} chars)")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        log(f"ERROR: {e}")
