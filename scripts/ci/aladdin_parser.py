# coding=utf-8
# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

import os
import shlex
import json
import re
from collections import defaultdict
from importlib import import_module

import yaml
import requests
from setuptools import find_packages
from git import Repo
# from pprint import pprint


# disable deprecation warning
yaml.warnings({'YAMLLoadWarning': False})

BASE_PATH = os.path.abspath(os.path.curdir)


def load_aladdin_helps():
    from azure.cli.command_modules import aladdin
    return {cmd: next(yaml.load_all(content)) for cmd, content in aladdin.aladdin_helps.items()}


def load_command_help_modules():
    command_modules_dir = os.path.join(BASE_PATH, 'src', 'azure-cli')

    mods = []

    pkgs = find_packages(where=command_modules_dir, exclude=['*test*'])
    for pkg in pkgs:
        try:
            m = import_module(pkg + '._help')
        except ModuleNotFoundError:
            continue

        mods.append(m)

    return mods


def format_examples(cmd, examples):
    formatted = defaultdict(list)

    for example in examples:
        text = example['text']
        command_line = text[text.index(cmd):]

        parameter_section = command_line[command_line.index(cmd) + len(cmd):]
        parameter_section = shlex.split(parameter_section)

        parameter_names = [p.strip() for p in parameter_section if p.startswith('--')]
        parameter_names.sort()

        formatted[tuple(parameter_names)].append(example)

    return formatted


def calculate_example_yaml_indent(raw_cli_help):
    yaml_indent_key = raw_cli_help[0]
    yaml_indent_idx = 0

    for idx, s in enumerate(raw_cli_help):
        if s.strip().startswith('examples'):
            yaml_indent_key = s
            yaml_indent_idx = idx
            break

    indent_width = re.search(r'[^ ]', yaml_indent_key).start()
    preceding_indent = yaml_indent_key[:indent_width]

    if 'examples' in yaml_indent_key:
        yaml_indent_key = raw_cli_help[yaml_indent_idx + 1]
        indent_width = re.search(r'[^ ]', yaml_indent_key).start()
        example_inner_indent = yaml_indent_key[:indent_width]
    else:
        example_inner_indent = preceding_indent + ' ' * 2

    return preceding_indent, example_inner_indent


def write_examples(cmd, examples, buffer, example_inner_indent):
    print('--- merge examples for: [ {:<35} ]'.format(cmd))

    two_space = '  '
    name_tpl = example_inner_indent + '- name: {}\n'
    text_tpl = example_inner_indent+ two_space + 'text: |\n'
    crafted_tpl = '\n' + example_inner_indent + two_space + 'crafted: true'
    cmd_tpl = example_inner_indent + two_space * 3 + 'az {}'
    opt_tpl = ' \\\\\n' + example_inner_indent + two_space * 3 + '{}'
    val_tpl = ' {}'

    for ex in examples:
        buffer.append(name_tpl.format(ex['name']))
        buffer.append(text_tpl)
        buffer.append(cmd_tpl.format(cmd))

        parameters = ex['text']
        parameters = parameters.replace('\\', '\\\\')
        parameters = parameters[parameters.index(cmd) + len(cmd):].strip()
        if len(parameters) + len(example_inner_indent) < 80:
            buffer.append(' ' + parameters)
        else:
            for p in parameters.split(' '):
                if p.startswith('--'):
                    buffer.append(opt_tpl.format(p))
                else:
                    buffer.append(val_tpl.format(p))

        if 'crafted' in ex:
            buffer.append(crafted_tpl)

        buffer.append('\n')


def merge_examples(cmd, raw_cli_help, aladdin_help, buffer):
    yaml_cli_help = next(yaml.load_all(''.join(raw_cli_help)))

    cli_examples = yaml_cli_help.get('examples', None)

    aladdin_examples = aladdin_help.get('examples')
    if aladdin_examples is None:
        return

    formatted_cli_examples = format_examples(cmd, cli_examples if cli_examples else [])
    formatted_aladdin_examples = format_examples(cmd, aladdin_examples)

    yaml_example_key_written = False

    # get preceding indent
    preceding_indent, example_inner_indent = calculate_example_yaml_indent(raw_cli_help)

    # append Aladdin added examples
    for parameter_seq, examples in formatted_aladdin_examples.items():
        if parameter_seq in formatted_cli_examples:
            continue

        if yaml_example_key_written is False and formatted_aladdin_examples and not formatted_cli_examples:
            buffer.append(preceding_indent + 'examples:\n')
            yaml_example_key_written = True

        write_examples(cmd, examples, buffer, example_inner_indent)


def merge(aladdin_generated_helps, help_module):
    print('==================== [ Processing: {:<50} ] ==================='.format(help_module.__name__))

    buffer = []

    with open(help_module.__file__, 'r', encoding='utf-8') as f:
        for line in f:
            if line.startswith("helps['") is False:
                buffer.append(line)
                continue

            buffer.append(line)

            cmd = re.search(r"helps\['(.*?)'\] = \"\"\"\n", line).groups()[0].strip()

            # start parseing help entries
            raw_yaml_body = []

            line = f.readline()
            while line.endswith('"""\n') is False:
                raw_yaml_body.append(line)
                line = f.readline()

            buffer.extend(raw_yaml_body)  # save the hardcode help entries first

            if cmd in aladdin_generated_helps:
                merge_examples(cmd, raw_yaml_body, aladdin_generated_helps[cmd], buffer)

            buffer.append(line)    # line == '""""\n'

    temp_file_name = help_module.__name__ + '.py'
    with open(temp_file_name, 'w', encoding='utf-8') as tmp:
        tmp.writelines(buffer)

    # apply changes to original file if there are, otherwise, nothing changed
    os.replace(temp_file_name, help_module.__file__)


def git_operation(modules):    # pylint: disable=redefined-outer-name
    print('\n==================== [ Processing Changes, commits, push, Pull Request ] ===================')

    target_repo_owner, target_repo, target_branch = 'Azure', 'azure-cli', 'dev'
    source_repo_owner, source_repo, source_branch = 'haroldrandom', 'azure-cli', 'Aladdin-dst'

    repo = Repo(BASE_PATH)
    git = repo.git

    try:
        current_branch = repo.active_branch.name
    except TypeError:
        # raise if HEAD is a detached symbolic reference
        git.checkout('-b', source_branch)
        current_branch = repo.active_branch.name

    has_changes = False

    # 1. commit changes if any
    for module in modules:
        short_name = module.__name__.split('.')[-2].capitalize()
        if git.diff(module.__file__):
            git.add(module.__file__)
            git.commit('-m', '[{}] Merge Aladdin generated examples'.format(short_name))

            has_changes = True

    if has_changes is False:
        print('No changes from Aladdin.')
        print('No need to push commits and fire Pull Request.')

    # 2. if changes, push commits to source repo
    # git.push('origin', '{}:{}'.format(current_branch, source_branch))

    # # 3. draft a pull request from source branch to target branch
    # headers = {
    #     'Authorization': 'token xxx',
    #     # 'Accept': 'application/vnd.github.shadow-cat-preview+json'  # draft PR needed
    # }
    # url = 'https://api.github.com/repos/{owner}/{repo}/pulls'.format(
    #         owner=target_repo_owner,repo=target_repo)
    # data = {
    #     'title': "[Aladdin Parser] Parse aladdin.py into commands' _help.py",
    #     'head': '{}:{}'.format(source_repo_owner, source_branch),
    #     'base': target_branch,
    #     'maintainer_can_modify': True,
    #     # 'draft': True
    # }
    # resp = requests.post(url=url, data=json.dumps(data), headers=headers)
    # resp.raise_for_status()


if __name__ == '__main__':
    aladdin_helps = load_aladdin_helps()
    # pprint(aladdin_helps)

    modules = load_command_help_modules()

    # test_mod = None
    # for mod in modules:
    #     if 'ams' in mod.__file__:
    #         test_mod = mod
    #         break
    # merge(aladdin_helps, test_mod)

    azure_cli_own_helps = [
        'azure.cli.command_modules.arm._help',
        'azure.cli.command_modules.cloud._help',
        'azure.cli.command_modules.monitor._help',
        'azure.cli.command_modules.network._help',
        'azure.cli.command_modules.storage._help',
        'azure.cli.command_modules.keyvault._help',
        'azure.cli.command_modules.vm._help',
        'azure.cli.command_modules.privatedns._help',
        'azure.cli.command_modules.resource._help'
    ]

    for mod in modules:
        # merge(aladdin_helps, mod)
        if mod.__name__ in azure_cli_own_helps:
            merge(aladdin_helps, mod)

    git_operation(modules)

