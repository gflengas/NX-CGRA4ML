"""Core clock/STA safeguards; synthetic vendor reports, no board or build assets."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import create_nx_cgra4ml_project as generator

ROOT = generator.REPO_ROOT
CLOCK = 'fabric_40mhz'
TARGET = 'rg~clk_50_0mhz'


def write_json(path, value):
    path.write_text(json.dumps(value) + '\n')


RISING = [('Rising', 'Rising')]
THREE_EDGES = [('Falling', 'Rising'), ('Rising', 'Falling'), ('Rising', 'Rising')]


def reports(directory, corner, edges=RISING):
    """Synthetic reports, deliberately independent of the acceptance code."""
    logs = directory / 'logsPython'
    logs.mkdir(exist_ok=True)
    constraints = dict(createClock=[dict(name=CLOCK, period=1000 / 40, targetPin=TARGET,
                                        isActivated=True, error='')])
    fields = []
    rows = []
    for source, target in edges:
        fields.append(dict(launchClockMode=source, latchClockMode=target,
                           holdSlack='1ns', setupSlack='2ns', holdArrival='1ns',
                           setupArrival='23ns', holdRelation='0ns', setupRelation='25ns'))
        rows.append(f'| {CLOCK} ({source}) | {CLOCK} ({target}) | 40.0 MHz | 43.5 MHz '
                    '| 1ns | 1ns | 0ns | 2ns | 23ns | 25ns | OK | - | OK |')
    analysis = dict(domainReports=[
        dict(state='Native', condition='', synthesisMode=False, domainContents=[]),
        dict(state='Routed', condition=corner, synthesisMode=False, domainContents=[
            dict(launchClock=CLOCK, latchClock=CLOCK, synchronous='true', userName='fabric',
                 requiredFrequency='40.0 MHz', actualFrequency='43.5 MHz', fieldContents=fields)])])
    write_json(logs / 'timingConstraints_Routed.json', constraints)
    write_json(logs / 'STAnalysisResults.json', analysis)
    (logs / f'Summary_Routed_{corner}.timing').write_text(
        f'Static Timing Analysis Conditions: {corner}\n' + '\n'.join(rows))
    (logs / f'Violation_summary_Routed_{corner}.timing').write_text('Nothing to report\n')


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config'
        self.config.mkdir()
        (self.config / 'config_hw.svh').write_text(
            '`define ROWS 8\n`define COLS 24\n`define Y_BITS 32\n'
            '`define Y_OUT_BITS 32\n`define X_BITS 8\n`define K_BITS 8\n'
            '`define AXI_WIDTH 128\n`define W_BPT 16\n')
        (self.config / 'config_hw.tcl').write_text('set FREQ 40\n')
        write_json(self.config / 'hardware.json', generator.HARDWARE_CONFIG)
        self.project = self.root / 'project'
        self.argv = ['--config-dir', str(self.config), '--project-dir', str(self.project), '--no-run']
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(generator.main(self.argv), 0)
        self.calls = []
        self.mutate = lambda directory, corner: None
        self.at_load = lambda directory: None
        self.failed_stage = None
        self.bad_registers = False
        self.edges = RISING
        self.ram_edge = 'Rising'
        self.child_failure = False
        self.child_commands = []

    def fake_nx(self):
        owner = self

        class Project:
            def __init__(self, directory):
                self.directory = Path(directory)

            def load(self, archive):
                owner.calls.append(('load', archive))
                owner.at_load(self.directory)

            def setDirectory(self, directory):
                self.directory = Path(directory)

            def createAnalyzer(self):
                return self

            def preAnalyze(self):
                reports(self.directory, 'worstcase', owner.edges)

            def launch(self, options):
                corner = options['conditions']
                owner.calls.append(('STA', corner))
                reports(self.directory, corner, owner.edges)
                owner.mutate(self.directory, corner)

            def reportRegisters(self, name):
                (self.directory / 'logsPython' / (name + '.log')).write_text(
                    f'Type: DFF\nClock Net: {TARGET}\nSensitivity: Rising\nBus Total: 2\n'
                    f'Type: RAM\nClock Net: {TARGET}\nSensitivity: {owner.ram_edge}\n'
                    'Bus Total: 2\nRegister Total: 2\n' if not owner.bad_registers else '')

            def progress(self, stage, steps):
                owner.calls.append(('progress', stage))
                return stage != owner.failed_stage

            def save(self, filename):
                Path(filename).write_text('synthetic routed archive')

            def generateBitstream(self, filename):
                owner.calls.append(('bitstream', filename))
                Path(filename).write_text('synthetic bitstream')

            def destroy(self):
                pass

            def __getattr__(self, name):
                if name not in {'setVariantName', 'setTopCellName', 'setDescription',
                                'addFile', 'createClock', 'setOptions', 'addMappingDirective'}:
                    raise AttributeError(name)
                return lambda *args, **kwargs: None

        module = types.ModuleType('nxpython')
        module.createProject = lambda directory: Project(directory)
        module.setDirAutoChange = lambda value: None
        module.getProject = lambda: None
        module.getClockNet = lambda target: target
        module.getModels = lambda target: target
        module.__all__ = [key for key in vars(module) if not key.startswith('_')]
        return module

    def run_flow(self, stop='Bitstream'):
        # Execute the staged flow, replacing only vendor API calls. Each corner
        # enters through the same CLI used by a real child process.
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(generator.main(self.argv + ['--clean', '--stop-after', stop]), 0)
        path = self.project / 'transcript.py'
        transcript = path.read_text()
        before_path = list(sys.path)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), before_path))
        def child(command, **kwargs):
            self.child_commands.append(command)
            self.assertEqual(command[0], '/isolated-vendor/bin/nxpython')
            self.assertEqual(Path(command[1]), self.project / 'flow.py')
            self.assertEqual(command[2], '--resume')
            self.assertTrue(kwargs['check'])
            if self.child_failure:
                raise subprocess.CalledProcessError(2, command)
            self.assertEqual(sys.modules['flow'].main(command[2:]), 0)
            return subprocess.CompletedProcess(command, 0)
        with patch.dict(os.environ, {'NANOXPLORE_PREFIX': '/isolated-vendor'}), \
                patch('subprocess.run', side_effect=child), \
                patch.dict(sys.modules, {'nxpython': self.fake_nx()}), \
                contextlib.redirect_stdout(io.StringIO()):
            sys.modules.pop('flow', None)
            exec(compile(transcript, str(path), 'exec'), {})

    def assert_blocked(self):
        with self.assertRaises((ValueError, RuntimeError, KeyError, FileNotFoundError,
                                FileExistsError, json.JSONDecodeError, SystemExit)):
            self.run_flow()
        self.assertFalse(any(name == 'bitstream' for name, _ in self.calls), self.calls)

    def test_full_passing_flow_orders_both_corners_before_generation(self):
        # flow.py is the generator at the preset's clock (regression4: 40 MHz).
        self.assertEqual((self.project / 'flow.py').read_text(),
                         Path(generator.__file__).read_text().replace(
                             '\nFABRIC_CLOCK_MHZ = 30\n', '\nFABRIC_CLOCK_MHZ = 40\n'))
        self.assertFalse((self.project / 'guards').exists())
        self.run_flow()
        self.assertEqual([value for name, value in self.calls if name == 'STA'],
                         ['worstcase', 'bestcase'])
        relevant = [name for name, _ in self.calls if name in ('STA', 'bitstream')]
        self.assertEqual(relevant, ['STA', 'STA', 'bitstream'])
        loaded = [value for name, value in self.calls if name == 'load']
        self.assertEqual(loaded, [str(self.project / 'routed-auto.nym')] * 3)
        self.assertTrue((self.project / 'qualified-bitstream/result.json').is_file())

        self.assertEqual(len(self.child_commands), 2)
        self.assertEqual([cmd[cmd.index('--conditions') + 1] for cmd in self.child_commands],
                         ['worstcase', 'bestcase'])
        self.assertNotEqual(self.child_commands[0], self.child_commands[1])

    def test_falling_edge_register_blocks_generation(self):
        self.ram_edge = 'Falling'
        self.assert_blocked()

    def test_falling_edge_relationships_block_generation(self):
        self.edges = THREE_EDGES
        self.assert_blocked()

    def test_child_failure_blocks_generation(self):
        self.child_failure = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_flow()
        self.assertEqual(len(self.child_commands), 1)
        self.assertFalse(any(name == 'bitstream' for name, _ in self.calls))

    def reject_negative(self, selected_corner, field_name):
        def mutate(directory, corner):
            if corner != selected_corner:
                return
            path = directory / 'logsPython/STAnalysisResults.json'
            data = json.loads(path.read_text())
            for field in data['domainReports'][1]['domainContents'][0]['fieldContents']:
                field[field_name] = '-1ns'
            write_json(path, data)
            summary = directory / f'logsPython/Summary_Routed_{corner}.timing'
            old, new = ('| 2ns |', '| -1ns |') if field_name == 'setupSlack' else (
                '| 1ns | 1ns |', '| -1ns | 1ns |')
            summary.write_text(summary.read_text().replace(old, new))
        self.mutate = mutate
        with self.assertRaisesRegex(ValueError, selected_corner + ' routed setup/hold failed'):
            self.run_flow()
        self.assertFalse(any(name == 'bitstream' for name, _ in self.calls))

    def test_negative_setup_stops_before_second_corner(self):
        self.reject_negative('worstcase', 'setupSlack')
        self.assertEqual([value for name, value in self.calls if name == 'STA'], ['worstcase'])

    def test_negative_bestcase_hold_blocks_generation(self):
        self.reject_negative('bestcase', 'holdSlack')

    def test_incomplete_registers_block_generation(self):
        self.bad_registers = True
        self.assert_blocked()

    def test_archive_changed_at_generation_boundary(self):
        def mutate(directory):
            if directory.name == 'qualified-bitstream':
                (self.project / 'routed-auto.nym').write_text('different archive')
        self.at_load = mutate
        self.assert_blocked()

    def test_configuration_changed_at_generation_boundary(self):
        def mutate(directory):
            if directory.name == 'qualified-bitstream':
                (self.project / 'rtl/config_hw.svh').write_text('different configuration')
        self.at_load = mutate
        self.assert_blocked()

    def test_stage_failures_and_stop_points(self):
        for stage in ('Synthesize', 'Place', 'Route'):
            with self.subTest(failure=stage):
                self.calls.clear()
                self.failed_stage = stage
                self.assert_blocked()
                self.assertEqual([value for name, value in self.calls if name == 'progress'][-1], stage)
            with self.subTest(stop=stage):
                self.calls.clear()
                self.failed_stage = None
                self.run_flow(stop=stage)
                self.assertFalse(any(name in ('STA', 'bitstream') for name, _ in self.calls))
                self.assertEqual([value for name, value in self.calls if name == 'progress'][-1], stage)

    def test_invalid_profiles_preserve_existing_project(self):
        sentinel = self.project / 'sentinel'
        sentinel.write_text('preserve')
        original = (self.config / 'hardware.json').read_text()
        for case in ('old-clock-option', 'external-pll', 'disabled-clock', 'missing-hardware',
                     'mismatch', 'wrong-width', 'wrong-ram'):
            with self.subTest(case=case):
                (self.config / 'hardware.json').write_text(original)
                argv = self.argv + ['--clean']
                if case == 'old-clock-option':
                    argv += ['--fabric-clock-mhz', '40']
                elif case == 'external-pll':
                    argv += ['--pll-config', 'unused.json']
                elif case == 'disabled-clock':
                    argv += ['--no-clock-constraint']
                elif case == 'missing-hardware':
                    (self.config / 'hardware.json').unlink()
                elif case in ('wrong-width', 'wrong-ram'):
                    hardware = json.loads(original)
                    hardware['bits_input' if case == 'wrong-width' else 'ram_edges_depth'] = (
                        8 if case == 'wrong-width' else 4)
                    write_json(self.config / 'hardware.json', hardware)
                else:
                    write_json(self.config / 'hardware.json', {'frequency_mhz': 40})
                result = subprocess.run([sys.executable, str(ROOT / 'create_nx_cgra4ml_project.py')]
                                        + argv, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(sentinel.read_text(), 'preserve')

    def test_hardware_export_requires_no_config_and_preserves_existing_files(self):
        destination = self.root / 'frontend' / 'hardware.json'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(generator.main(['--export-hardware', str(destination),
                                            '--config-dir', str(self.root / 'absent')]), 0)
        self.assertEqual(json.loads(destination.read_text()), generator.HARDWARE_CONFIG)
        original = destination.read_bytes()
        with self.assertRaises(FileExistsError):
            generator.main(['--export-hardware', str(destination)])
        self.assertEqual(destination.read_bytes(), original)

    def test_pll_and_sta_frequencies_match(self):
        profile = generator.get_pll_config()
        self.assertEqual((profile['divided_reference_mhz'], profile['vco_mhz'],
                          profile['output_mhz']), (20, 600, 40))
        with patch.dict(generator.PLL_CONFIG, {'fbk_intdiv': 13}), \
                self.assertRaises(AssertionError):
            generator.get_pll_config()
        transcript = (self.project / 'transcript.py').read_text()
        self.assertIn('period=' + repr(1000 / 40), transcript)
        self.assertIn('name="fabric_40mhz"', transcript)


if __name__ == '__main__':
    unittest.main()
