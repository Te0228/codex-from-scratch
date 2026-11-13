"""Shared implementation modules used by the runtime and textbook."""
import importlib

DOMAINS = {
    '02': 'context', '03': 'transport', '04': 'tools', '05': 'processes',
    '06': 'scheduling', '07': 'configuration', '08': 'permissions', '09': 'sandbox',
    '10': 'sessions', '11': 'memory', '12': 'goals', '13': 'skills', '14': 'mcp',
    '15': 'plugins', '16': 'orchestration', '17': 'agents', '18': 'rpc',
    '19': 'clients', '20': 'environments', '21': 'hosts', '22': 'artifacts',
    '23': 'media', '24': 'realtime', '25': 'search', '26': 'computer_use',
    '27': 'telemetry', '28': 'cache', '29': 'evaluation', '30': 'distribution',
    '31': 'assembly',
}


def load_component(chapter, module):
    chapter = str(chapter).removeprefix('ch').zfill(2)
    if chapter not in DOMAINS or not module.isidentifier():
        raise ValueError('Unknown component')
    return importlib.import_module(f'mini_codex.components.{DOMAINS[chapter]}.{module}')
