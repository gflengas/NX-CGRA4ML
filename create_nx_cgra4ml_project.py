#!/usr/bin/env python3
"""Create and optionally run a clean NanoXplore CGRA4ML project.

The generated project lives under NX_cgra4ml by default.  It stages the repo RTL
tree, writes a local nxpython transcript, and runs the known working Impulse
flow: synthesize, place, route, then accept both routed timing corners before
generating bitstream.nxb.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
RTL_SRC = REPO_ROOT / "CGRA4ML" / "deepsocflow" / "rtl"
DEFAULT_NXPYTHON = "/opt/NanoXplore/nxdesignsuite-24.3.0.0/bin/nxpython"
# Build clocks. Keep PLL, STA and exported hardware metadata consistent; the
# divider values specify a nominal, not measured, frequency. The preset selects
# the clock; a build's flow.py copy carries it in the FABRIC_CLOCK_MHZ line below.
CLOCK_PROFILES = {
    30: {"reference_mhz": 400, "ref_intdiv": 15, "fbk_intdiv": 8, "clk_outdiv1": 6, "pll_odf": 0},
    # 20 MHz divided reference, 600 MHz VCO, / 15.
    40: {"reference_mhz": 400, "ref_intdiv": 19, "fbk_intdiv": 14, "clk_outdiv1": 6, "pll_odf": 0},
}
FABRIC_CLOCK_MHZ = 30
FABRIC_CLOCK_TARGET = "rg~clk_50_0mhz"


def configure_clock(mhz: int) -> None:
    """Selects a build clock profile (module-wide)."""
    global FABRIC_CLOCK_MHZ, FABRIC_CLOCK_NAME, FABRIC_PERIOD_NS, PLL_CONFIG
    FABRIC_CLOCK_MHZ = mhz
    FABRIC_CLOCK_NAME = f"fabric_{mhz:g}mhz"
    FABRIC_PERIOD_NS = 1000.0 / mhz
    PLL_CONFIG = dict(CLOCK_PROFILES[mhz])


configure_clock(FABRIC_CLOCK_MHZ)
# The accelerator hardware of preset regression4. Export a preset's
# hardware.json for the frontend so payload packing and the hardware build use
# the same dimensions, numeric widths and clock.
HARDWARE_CONFIG = {
    "processing_elements": [8, 24],
    "frequency_mhz": 40,
    "bits_input": 4,
    "bits_weights": 4,
    "bits_sum": 20,
    "bits_bias": 16,
    "max_batch_size": 64,
    "max_channels_in": 512,
    "max_kernel_size": 9,
    "max_image_size": 512,
    "max_n_bundles": 64,
    "ram_weights_depth": 512,
    "ram_edges_depth": 3584,
    "axi_width": 128,
    "header_width": 64,
    "config_baseaddr": "B0000000",
    "axi_max_burst_len": 16,
    "target_cpu_int_bits": 32,
    "async_resetn": True,
    "valid_prob": 1,
    "ready_prob": 1,
    "data_dir": "vectors",
}
# The qualified images, one preset each: hardware.json plus the build options
# that closed timing. Seed None is the tool's default (1789). Impulse maps the
# multipliers (DSPs for 8 bits, logic for 4 bits).
#   regression4  ResNet and U-Net exports: 4-bit inputs and weights, kernel 9,
#                image 512, 40 MHz
HARDWARE_PRESETS = {
    "regression4": dict(hardware=HARDWARE_CONFIG, seed=None),
}
ROUTING_EFFORT = "High"
MIN_MAX_REGISTER_COUNT = 20_000
MARKER = ".generated-by-create_nx_cgra4ml_project"


def get_pll_config() -> dict:
    """Explicit NG-ULTRA internal-reference, internal-feedback CLK_DIV1 profile.

    NanoXplore's PLL calculator uses VCO = 2 * REF/(R+1) * (F+1).
    The shell uses a regularized, undivided WFG on CLK_DIV1.
    This is a nominal configuration calculation, not a frequency measurement.
    """
    settings = PLL_CONFIG.copy()
    assert settings["reference_mhz"] == 400
    for key, maximum in (("ref_intdiv", 31), ("fbk_intdiv", 127),
                         ("clk_outdiv1", 7), ("pll_odf", 3)):
        assert 0 <= settings[key] <= maximum, key
    ref_mhz = settings["reference_mhz"] / (settings["ref_intdiv"] + 1)
    vco_mhz = 2 * ref_mhz * (settings["fbk_intdiv"] + 1)
    # Installed NG-ULTRA family datasheet v1.02, Table 17. Input limits apply
    # after the reference divider. These limits do not establish physical STA.
    assert 10 <= ref_mhz <= 50 and 300 <= vco_mhz <= 800, "PLL outside datasheet limits"
    odf = (1, 2, 5, 10)[settings["pll_odf"]]
    output_mhz = vco_mhz / odf / (2 * settings["clk_outdiv1"] + 3)
    assert math.isclose(output_mhz, FABRIC_CLOCK_MHZ, rel_tol=0, abs_tol=1e-9)
    return dict(settings, divided_reference_mhz=ref_mhz, vco_mhz=vco_mhz,
                output_mhz=output_mhz)





RTL_FILES = [
    "ext/alex_axis_pipeline_register.v",
    "ext/alex_axis_register.v",
    "axi_cgra4ml_2m.v",
    "axi_cgra4ml.v",
    "dnn_engine.v",
    # NG-ULTRA-native write-first RAM, cycle-identical to upstream's
    # xilinx_spwf.v template (whose read Impulse emulated in logic).
    "nx_rams_sp_wf.sv",
    "ngultra_cgra4ml_top.v",
    "ext/alex_axi_dma_wr.sv",
    "ext/alex_axi_dma_rd.sv",
    "ext/alex_axis_adapter_any.sv",
    "ext/alex_axis_adapter.sv",
    "axis_out_shift.sv",
    "axis_pixels.sv",
    "axis_weight_rotator.sv",
    "counter.sv",
    "cyclic_bram.sv",
    "dma_controller.sv",
    "n_delay.sv",
    "proc_engine.sv",
    "ram.sv",
    "skid_buffer.sv",
    "ngultra_llpp_regs.v",
    "ngultra_read_reserve.v",
    "ext/xilinx_sdp.sv",
]
SHELL = "ngultra_cgra4ml_top.v"


def write_runtime_line(message: str, runtime_log_file) -> None:
    print(message, flush=True)
    runtime_log_file.write(f"{message}\n")
    runtime_log_file.flush()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_clock(directory, name, period, target):
    paths = list((directory / 'logsPython').glob('timingConstraints_*.json'))
    if not paths:
        raise RuntimeError('timing constraint report is missing')
    clocks_by_report = {}
    for path in paths:
        report = json.loads(path.read_text())
        clocks = report.get('createClock', []) + report.get('createGeneratedClock', [])
        if len(clocks) != 1:
            raise RuntimeError(f'{path}: expected one clock, got {len(clocks)}')
        clock = clocks[0]
        if (clock['name'] != name or clock['period'] != period or
                clock['targetPin'] != target or clock['isActivated'] is not True or clock['error']):
            raise RuntimeError(f'{path}: missing, inactive or changed clock: {clock}')
        clocks_by_report[path.name] = clock
    return clocks_by_report


def check_register_report(path, target):
    # reportRegisters() without a filename only logs its live data. The generic
    # JSON summary can remain empty after preAnalyze(); request an explicit file.
    content = path.read_text()
    groups = re.findall(r'Type:\s+(\S+).*?Clock Net:\s+(\S+).*?Sensitivity:\s+(\S+)',
                        content, re.S)
    totals = [int(value) for value in re.findall(r'Bus Total:\s+(\d+)', content)]
    bits = [int(value) for value in re.findall(r'Register Total:\s+(\d+)', content)]
    if (not groups or len(groups) != len(re.findall(r'\bType:', content))
            or not totals or set(totals) != {len(groups)} or len(bits) != 1
            or bits[0] <= 0):
        raise RuntimeError('explicit register report is empty or incomplete')
    # Every register (flip-flops and the NG-ULTRA RAM) on the approved clock,
    # rising edge.
    if any(clock != target or (kind, edge) not in {('DFF', 'Rising'), ('RAM', 'Rising')}
           for kind, clock, edge in groups):
        raise RuntimeError('a register is off the approved clock or not rising-edge')
    return dict(register_groups=len(groups), register_bits=bits[0],
                report_sha256=digest(path))


def resume_main(argv=None):
    parser = argparse.ArgumentParser(description="Resume a pinned archive (at this flow's clock) into a new directory; never generate a bitstream.")
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--archive-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--stage', choices=('Place', 'Route', 'STA'), required=True)
    parser.add_argument('--conditions', choices=('worstcase', 'bestcase'))
    args = parser.parse_args(argv)
    if (args.stage == 'STA') != (args.conditions is not None):
        parser.error('--conditions is required only for STA')
    archive, output = args.archive.resolve(), args.output.resolve()
    if digest(archive) != args.archive_sha256:
        raise ValueError('input archive hash differs from the approved pin')
    output.mkdir(parents=True, exist_ok=False)
    result = dict(state='running', pid=os.getpid(), started_unix=time.time(),
                  stage=args.stage, conditions=args.conditions, archive=str(archive),
                  archive_sha256=args.archive_sha256, script_sha256=digest(Path(__file__)))
    result_path = output / 'result.json'
    result_path.write_text(json.dumps(result, indent=2) + '\n')
    from nxpython import createProject, setDirAutoChange
    project = None
    try:
        setDirAutoChange(False)
        project = createProject(str(output))
        project.load(str(archive))
        project.setDirectory(str(output))
        analyzer = project.createAnalyzer()
        analyzer.preAnalyze()
        result['input_clocks'] = check_clock(output, FABRIC_CLOCK_NAME, FABRIC_PERIOD_NS,
                                            FABRIC_CLOCK_TARGET)
        if args.stage == 'STA':
            if set(result['input_clocks']) != {'timingConstraints_Routed.json'}:
                raise RuntimeError('STA requires a routed archive')
            analyzer.launch({'maximumSlack': 500, 'searchPathsLimit': 100,
                             'conditions': args.conditions})
            reports = output / 'logsPython'
            for name in (f'Summary_Routed_{args.conditions}.timing', 'STAnalysisResults.json'):
                if not (reports / name).is_file():
                    raise RuntimeError(f'STA report is missing: {name}')
            result['timing_acceptance'] = 'pending report review'
        else:
            steps = {'Place': 5, 'Route': 3}[args.stage]
            ok = project.progress(args.stage, steps)
            checkpoint = output / f'{args.stage.lower()}.nym'
            project.save(str(checkpoint))
            result['checkpoint'] = str(checkpoint)
            result['checkpoint_sha256'] = digest(checkpoint)
            result['progress_success'] = bool(ok)
            if not ok:
                raise RuntimeError(f'{args.stage} {steps} failed; checkpoint retained')
            project.createAnalyzer().preAnalyze()
        result['output_clocks'] = check_clock(output, FABRIC_CLOCK_NAME, FABRIC_PERIOD_NS,
                                             FABRIC_CLOCK_TARGET)
        project.reportRegisters('current-registers')
        result.update(check_register_report(output / 'logsPython/current-registers.log',
                                            FABRIC_CLOCK_TARGET))
        result['state'] = 'passed'
    except BaseException as error:
        result['state'] = 'failed'
        result['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        try:
            if project is not None:
                project.destroy()
        finally:
            result['input_unchanged'] = digest(archive) == args.archive_sha256
            result['finished_unix'] = time.time()
            if not result['input_unchanged']:
                result['state'] = 'failed'
                result['error'] = 'input archive mutated'
            result_path.write_text(json.dumps(result, indent=2) + '\n')
            if not result['input_unchanged']:
                raise RuntimeError('input archive mutated')
    print(f'{args.stage} completed; design/constraints unchanged; acceptance requires report review')
    return result



def nanoseconds(value):
    match = re.fullmatch(r'([+-]?\d+(?:\.\d+)?)(ps|ns)', value)
    if not match:
        raise ValueError(f'missing or unsupported timing value: {value!r}')
    result = float(match[1]) * (0.001 if match[2] == 'ps' else 1)
    if not math.isfinite(result):
        raise ValueError('nonfinite timing value')
    return result


def review(directory, corner, clock_name, period, clock_target):
    logs = directory / 'logsPython'
    constraints_path = logs / 'timingConstraints_Routed.json'
    constraints = json.loads(constraints_path.read_text())
    if any(value for key, value in constraints.items()
           if key not in ('createClock', 'createGeneratedClock', 'm_header')):
        raise ValueError('unexpected timing directives or exceptions')
    clocks = constraints.get('createClock', []) + constraints.get('createGeneratedClock', [])
    if len(clocks) != 1:
        raise ValueError('expected one active fabric clock')
    clock = clocks[0]
    if (clock['isActivated'] is not True or clock['error'] or clock['name'] != clock_name or
            clock['period'] != period or clock['targetPin'] != clock_target):
        raise ValueError(f'inactive, unresolved or changed fabric clock: {clock}')
    path = logs / 'STAnalysisResults.json'
    analysis = json.loads(path.read_text())
    reports = analysis['domainReports']
    selected = [report for report in reports
                if report['state'] == 'Routed' and report['condition'] == corner]
    if len(selected) != 1:
        raise ValueError('analysis is not the requested routed corner')
    # preAnalyze() creates an empty Native record before launch() appends the
    # actual routed corner. It contains no analyzed domains and is not timing.
    if any(report is not selected[0] and
           (report['state'] != 'Native' or report['condition'] or report['domainContents'])
           for report in reports):
        raise ValueError('unexpected additional timing analysis record')
    if selected[0]['synthesisMode'] is not False:
        raise ValueError('synthesis timing estimate cannot qualify routed timing')
    domains = selected[0]['domainContents']
    if len(domains) != 1:
        raise ValueError('expected exactly one fabric timing domain')
    fields = []
    for domain in domains:
        if (domain['launchClock'] != clock_name or domain['latchClock'] != clock_name or
                domain['synchronous'] != 'true'):
            raise ValueError(f'unexpected or asynchronous domain: {domain["userName"]}')
        frequency = re.fullmatch(r'(\d+(?:\.\d+)?) MHz', domain['requiredFrequency'])
        if (not frequency or not math.isfinite(float(frequency[1]))
                or abs(float(frequency[1]) - 1000 / period) > 0.051):
            raise ValueError('reported required frequency differs from the approved target')
        if not domain['fieldContents']:
            raise ValueError('timing domain has no analyzed edge relationships')
        for field in domain['fieldContents']:
            fields.append(dict(domain=domain['userName'], source_edge=field['launchClockMode'],
                               target_edge=field['latchClockMode'],
                               setup_ns=nanoseconds(field['setupSlack']),
                               hold_ns=nanoseconds(field['holdSlack']),
                               setup_relation_ns=nanoseconds(field['setupRelation']),
                               hold_relation_ns=nanoseconds(field['holdRelation']),
                               maximum_arrival_ns=nanoseconds(field['setupArrival']),
                               minimum_arrival_ns=nanoseconds(field['holdArrival']),
                               reported_actual_frequency=domain['actualFrequency']))
    edges = [(field['source_edge'], field['target_edge']) for field in fields]
    # Only rising-edge registers: one rising-to-rising relationship.
    if edges != [('Rising', 'Rising')]:
        raise ValueError('fabric edge relationships other than rising-to-rising')
    passed = all(field['setup_ns'] >= 0 and field['hold_ns'] >= 0 for field in fields)
    files = [path, constraints_path, logs / f'Summary_Routed_{corner}.timing',
             logs / f'Violation_summary_Routed_{corner}.timing']
    summary = files[2].read_text()
    violated = files[3].read_text()
    if f'Static Timing Analysis Conditions: {corner}' not in summary:
        raise ValueError('text summary has missing or different corner')
    rows = []
    pattern = (r'\|\s*' + re.escape(clock_name) + r' \((Rising|Falling)\)\s*'
               r'\|\s*' + re.escape(clock_name) + r' \((Rising|Falling)\)\s*\|([^\n]+)')
    for source, target, tail in re.findall(pattern, summary):
        values = [value.strip() for value in tail.split('|')]
        rows.append((source, target, values))
    if len(rows) != len(fields) or {(s, t) for s, t, _ in rows} != set(edges):
        raise ValueError('text summary edge coverage differs from JSON')
    for source, target, values in rows:
        field = next(f for f in fields if (f['source_edge'], f['target_edge']) == (source, target))
        for index, key in ((2, 'hold_ns'), (3, 'minimum_arrival_ns'), (4, 'hold_relation_ns'),
                           (5, 'setup_ns'), (6, 'maximum_arrival_ns'), (7, 'setup_relation_ns')):
            if nanoseconds(values[index]) != field[key]:
                raise ValueError('text summary timing differs from JSON')
        if passed and (values[8:11] != ['OK', '-', 'OK']):
            raise ValueError('text summary does not accept timing')
    if passed and ('| KO' in summary or 'Nothing to report' not in violated):
        raise ValueError('JSON slacks and textual violation summary disagree')
    return dict(status='passed' if passed else 'failed', condition=corner,
                stage='Routed', active_clock=clock, edge_relationships=fields,
                minimum_setup_ns=min(field['setup_ns'] for field in fields),
                minimum_hold_ns=min(field['hold_ns'] for field in fields),
                sha256={str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in files})


def review_sta_main(argv=None):
    parser = argparse.ArgumentParser(description="Review retained routed STA reports (at this flow's clock) without vendor tools.")
    parser.add_argument('directory', type=Path)
    parser.add_argument('--conditions', choices=('worstcase', 'bestcase'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    report = review(args.directory, args.conditions, FABRIC_CLOCK_NAME, FABRIC_PERIOD_NS,
                    FABRIC_CLOCK_TARGET)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(f'{args.conditions} {report["status"]}: setup {report["minimum_setup_ns"]:.3f} ns; '
          f'hold {report["minimum_hold_ns"]:.3f} ns')
    return 0 if report['status'] == 'passed' else 1



def verify_manifest(project_dir, expected=None):
    path = project_dir / 'staging-manifest.json'
    actual = digest(path)
    if expected is not None and actual != expected:
        raise ValueError('staging manifest changed')
    manifest = json.loads(path.read_text())
    pins = manifest['sha256']
    required = [project_dir / 'transcript.py', project_dir / 'rtl/config_hw.svh',
                project_dir / 'rtl/config_hw.tcl',
                project_dir / 'rtl' / SHELL]
    required.append(project_dir / 'flow.py')
    if not pins or any(str(path) not in pins for path in required):
        raise ValueError('staging manifest is missing required input pins')
    for name, pin in pins.items():
        if digest(Path(name)) != pin:
            raise ValueError(f'staged input changed: {name}')
    frequency = manifest['fabric_clock_mhz']
    if frequency != FABRIC_CLOCK_MHZ or manifest['pll'] != get_pll_config():
        raise ValueError('manifest requires matching physical and STA clocks')
    return actual


def accept_corner(directory, archive, archive_hash, clock_name, period, target):
    result_path = directory / 'result.json'
    result = json.loads(result_path.read_text())
    corner = directory.name[4:] if directory.name.startswith('sta-') else ''
    if (corner not in ('worstcase', 'bestcase') or result['state'] != 'passed'
            or result['stage'] != 'STA' or result['conditions'] != corner
            or result['archive'] != str(archive)
            or result['archive_sha256'] != archive_hash
            or result['input_unchanged'] is not True
            or result['script_sha256'] != digest(Path(__file__))):
        raise ValueError('STA result is stale or belongs to another archive/run')
    registers = check_register_report(directory / 'logsPython/current-registers.log', target)
    if any(result[key] != value for key, value in registers.items()):
        raise ValueError('STA register evidence changed')
    reviewed = review(directory, corner, clock_name, period, target)
    if reviewed['status'] != 'passed':
        raise ValueError(f'{corner} routed setup/hold failed: '
                         f'{reviewed["minimum_setup_ns"]}/{reviewed["minimum_hold_ns"]} ns')
    reviewed['registers'] = registers
    reviewed['result_sha256'] = digest(result_path)
    return reviewed


def verify_acceptance(project_dir, acceptance):
    verify_manifest(project_dir, acceptance['manifest_sha256'])
    archive = project_dir / 'routed-auto.nym'
    if digest(archive) != acceptance['archive_sha256']:
        raise ValueError('routed archive changed')
    if acceptance['archive'] != str(archive):
        raise ValueError('acceptance belongs to another project')
    manifest = json.loads((project_dir / 'staging-manifest.json').read_text())
    frequency = manifest['fabric_clock_mhz']
    reviews = {}
    for corner in ('worstcase', 'bestcase'):
        reviews[corner] = accept_corner(project_dir / f'sta-{corner}', archive,
                                       acceptance['archive_sha256'],
                                       f'fabric_{frequency:g}mhz', 1000 / frequency,
                                       'rg~clk_50_0mhz')
    if reviews != acceptance['corners']:
        raise ValueError('timing evidence changed after acceptance')
    return reviews


def qualify_and_generate(project_dir, manifest_hash):
    """Analyze the saved archive twice, then load that same archive to generate.

    Fresh directories prevent a previous run's success from supplying missing
    reports. Acceptance is constructed here, never loaded from a passed flag.
    """
    project_dir = project_dir.resolve()
    verify_manifest(project_dir, manifest_hash)
    archive = project_dir / 'routed-auto.nym'
    archive_hash = digest(archive)
    manifest = json.loads((project_dir / 'staging-manifest.json').read_text())
    frequency = manifest['fabric_clock_mhz']
    name, period, target = f'fabric_{frequency:g}mhz', 1000 / frequency, 'rg~clk_50_0mhz'
    acceptance = dict(archive=str(archive), archive_sha256=archive_hash,
                      manifest_sha256=manifest_hash, corners={})
    for corner in ('worstcase', 'bestcase'):
        verify_manifest(project_dir, manifest_hash)
        output = project_dir / f'sta-{corner}'
        # A separate vendor process per corner: no report state leaks between them.
        command = [str(Path(os.environ['NANOXPLORE_PREFIX']) / 'bin/nxpython'),
                   str(Path(__file__).resolve()), '--resume', '--archive', str(archive),
                   '--archive-sha256', archive_hash, '--output', str(output), '--stage', 'STA',
                   '--conditions', corner]
        with (project_dir / f'sta-{corner}-process.log').open('x') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        acceptance['corners'][corner] = accept_corner(output, archive, archive_hash,
                                                     name, period, target)
    verify_acceptance(project_dir, acceptance)
    # Retain acceptance before generation. It is evidence, not a reusable waiver.
    with (project_dir / 'timing-acceptance.json').open('x') as stream:
        stream.write(json.dumps(acceptance, indent=2) + '\n')
    from nxpython import createProject, setDirAutoChange
    output = project_dir / 'qualified-bitstream'
    output.mkdir(exist_ok=False)
    setDirAutoChange(False)
    project = createProject(str(output))
    try:
        project.load(str(archive))
        project.setDirectory(str(output))
        verify_acceptance(project_dir, acceptance)
        project.generateBitstream(str(output / 'bitstream.nxb'))
        if not (output / 'bitstream.nxb').is_file():
            raise RuntimeError('bitstream generation did not produce its output')
        project.save(str(output / 'cgra4ml.nym'))
        acceptance['bitstream_sha256'] = digest(output / 'bitstream.nxb')
        verify_acceptance(project_dir, {key: value for key, value in acceptance.items()
                                       if key != 'bitstream_sha256'})
        (output / 'result.json').write_text(json.dumps(acceptance, indent=2) + '\n')
    finally:
        project.destroy()


# nxpython transcript of a build (main fills it in). The mapping directive puts
# the read-reservation FIFO memories in NX_RAM blocks: as register files they
# congest routing near the SoC ports.
TRANSCRIPT_TEMPLATE = '''import sys
from pathlib import Path
from nxpython import *


PROJECT_DIR = {project_dir}
RTL_FILES = {rtl_files}
sys.path.insert(0, PROJECT_DIR)
from flow import verify_manifest, qualify_and_generate
manifest_hash = verify_manifest(Path(PROJECT_DIR))


if getProject() is not None:
    getProject().destroy()

p = createProject(PROJECT_DIR)
p.setVariantName("NG-ULTRA", "")
p.setTopCellName("work", "ngultra_cgra4ml_top")
p.setDescription("Generated CGRA4ML NG-ULTRA project")

for rtl_file in RTL_FILES:
    p.addFile("work", rtl_file)

p.save(PROJECT_DIR + "/native-auto.nym")

# clk_50_0mhz is an inherited net name, not the physical clock frequency.
# The router regularizes CLK_DIV1 as rg~clk_50_0mhz through an undivided WFG.
p.createClock(
    target=getClockNet("{clock_target}"),
    name="{clock_name}",
    period={period},
)


p.setOptions({options})

p.addMappingDirective('getInstances(.*READ_RESERVE.*mem)', 'RAM', "RAM")

{stages}
if p is not None:
    p.destroy()
'''
STAGE_TEMPLATE = '''
ok = p.progress("{stage}", {steps})
p.save(PROJECT_DIR + "/{archive}-auto.nym")
if not ok:
    p.destroy()
    sys.exit(1)
'''
BITSTREAM_STAGE = '''
p.destroy()
p = None
qualify_and_generate(Path(PROJECT_DIR), manifest_hash)
'''


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--resume"]:
        resume_main(argv[1:])
        return 0
    if argv[:1] == ["--review-sta"]:
        return review_sta_main(argv[1:])
    parser = argparse.ArgumentParser(
        description="Stage and build a NanoXplore CGRA4ML project for one qualified image preset.",
        epilog="Use --resume --help for checkpoint execution, or --review-sta --help for report review.",
    )
    parser.add_argument("--export-hardware", type=Path,
        help="Write the selected preset's hardware.json to a new file and exit; no vendor tools.")
    parser.add_argument("--project-dir", help="New project directory (required to build).")
    parser.add_argument("--config-dir",
        help="Export directory with config_hw.svh, config_hw.tcl and hardware.json "
             "(required to build).")
    parser.add_argument("--nxpython", default=DEFAULT_NXPYTHON)
    parser.add_argument("--clean", action="store_true",
        help="Replace an existing generated project directory.")
    parser.add_argument("--no-run", action="store_true",
        help="Only stage RTL and write transcript.py; do not launch nxpython.")
    parser.add_argument("--stop-after", choices=("Synthesize", "Place", "Route", "Bitstream"),
        default="Bitstream", help="Stop after the selected successful vendor stage.")
    parser.add_argument("--seed", type=int, default=None,
        help="NanoXplore place/route random seed (default: the preset's).")
    parser.add_argument("--hardware-preset", choices=sorted(HARDWARE_PRESETS), default="regression4",
        help="Qualified image preset: hardware.json must match it (default: regression4).")
    args = parser.parse_args(argv)
    build = HARDWARE_PRESETS[args.hardware_preset]
    preset = build["hardware"]
    configure_clock(preset["frequency_mhz"])
    seed = build["seed"] if args.seed is None else args.seed
    pll_settings = get_pll_config()

    if args.export_hardware is not None:
        destination = args.export_hardware.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(preset, indent=2) + "\n")
        print(f"Exported hardware preset: {destination}")
        return 0

    if args.project_dir is None or args.config_dir is None:
        parser.error("--project-dir and --config-dir are required to build")
    project_dir = Path(args.project_dir).expanduser().resolve()
    config_dir = Path(args.config_dir).expanduser().resolve()
    config_hw_src = config_dir / "config_hw.svh"
    config_hw_tcl_src = config_dir / "config_hw.tcl"
    if json.loads((config_dir / "hardware.json").read_text()) != preset:
        parser.error(f"hardware.json must match the {args.hardware_preset} preset in this "
                     "script; use --export-hardware to prepare the model export's preset")
    config_hw = {}
    for line in config_hw_src.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*`define\s+([A-Z0-9_]+)\s+([0-9]+)\b", line)
        if match:
            config_hw[match.group(1)] = int(match.group(2))
    max_register_count = max(MIN_MAX_REGISTER_COUNT, config_hw["ROWS"] * config_hw["COLS"]
        * max(config_hw["Y_BITS"], config_hw["Y_OUT_BITS"]) * 24)

    # A fresh project directory; --clean replaces only one this script made.
    if project_dir.exists():
        if not args.clean:
            raise FileExistsError(f"{project_dir} already exists. Re-run with --clean to "
                                  "replace a previously generated project.")
        if not (project_dir / MARKER).exists():
            raise RuntimeError(f"Refusing to remove {project_dir}: missing {MARKER} marker.")
        shutil.rmtree(project_dir)
    project_dir.mkdir(parents=True)
    (project_dir / MARKER).write_text("Generated by create_nx_cgra4ml_project.py\n",
                                      encoding="utf-8")

    # Stage the RTL, then generate the shell's constants and PLL settings in
    # the copy (the source shell is never edited).
    rtl_dir = project_dir / "rtl"
    shutil.copytree(RTL_SRC, rtl_dir)
    shutil.copy2(config_hw_src, rtl_dir / "config_hw.svh")
    shutil.copy2(config_hw_tcl_src, rtl_dir / "config_hw.tcl")
    assert all((rtl_dir / path).is_file() for path in [*RTL_FILES, "defines.svh"])
    top = rtl_dir / SHELL
    source = top.read_text(encoding="utf-8")
    constants = {
        "C_ROWS": config_hw["ROWS"],
        "C_COLS": config_hw["COLS"],
        "C_X_BITS": config_hw["X_BITS"],
        "C_K_BITS": config_hw["K_BITS"],
        "C_Y_BITS": config_hw["Y_BITS"],
        "C_Y_OUT_BITS": config_hw["Y_OUT_BITS"],
        "C_AXI_WIDTH": config_hw["AXI_WIDTH"],
        "C_AXI_STRB_WIDTH": config_hw["AXI_WIDTH"] // 8,
        "C_W_BPT": config_hw["W_BPT"],
    }
    for name, value in constants.items():
        source, count = re.subn(rf"(localparam\s+integer\s+{name}\s*=\s*)[0-9]+(\s*;)",
                                rf"\g<1>{value}\2", source, count=1)
        if count != 1:
            raise RuntimeError(f"Could not patch {name} in {top}")
    # The shell's PLL: internal reference and feedback (get_pll_config).
    start = source.index("  NX_PLL_U\n")
    end = source.index("    i_fabric_pll (", start)
    generics = source[start:end]
    for key, value in (("ref_osc_on", "1'b1"), ("use_pll", "1'b1"),
                       ("ext_fbk_on", "1'b0"), ("fbk_delay_on", "1'b0")):
        if not re.search(rf"\.{key}\s*\({re.escape(value)}\)", generics):
            raise ValueError(f"PLL profile requires shell {key} = {value}")
    for key, bits in (("ref_intdiv", 5), ("fbk_intdiv", 7), ("clk_outdiv1", 3)):
        generics, count = re.subn(rf"(\.{key}\s*\({bits}'d)[0-9]+(\))",
                                  rf"\g<1>{pll_settings[key]}\2", generics)
        if count != 1:
            raise ValueError(f"cannot generate PLL parameter {key}")
    if re.search(r"\.pll_odf\s*\(", generics):
        raise ValueError("shell already sets pll_odf; review before replacing it")
    generics = generics.replace("    #(\n", "    #(\n"
        f"      .pll_odf      (2'd{pll_settings['pll_odf']}),\n", 1)
    top.write_text(source[:start] + generics + source[end:], encoding="utf-8")

    # flow.py: this script at the preset's clock, imported by the transcript.
    flow = project_dir / "flow.py"
    shutil.copy2(Path(__file__).resolve(), flow)
    flow_source = flow.read_text()
    if flow_source.count("\nFABRIC_CLOCK_MHZ = 30\n") != 1:
        raise ValueError("cannot set the build clock in flow.py")
    flow.write_text(flow_source.replace("\nFABRIC_CLOCK_MHZ = 30\n",
                                        f"\nFABRIC_CLOCK_MHZ = {FABRIC_CLOCK_MHZ}\n"))

    # The nxpython transcript: synthesize, place and route up to --stop-after,
    # then qualify both routed corners and generate the bitstream.
    options = {
        "ManageUnconnectedOutputs": "Ground",
        "ManageUnconnectedSignals": "Ground",
        "MaxRegisterCount": str(max_register_count),
        "MappingEffort": "High",
        "ManageAsynchronousReadPort": "Yes",
        "RoutingEffort": ROUTING_EFFORT,
        "OptimizedMux": "Yes",
        "VariantRepairSynthesis": "Yes",
        "TimingDriven": "Yes",  # both routed corners must pass regardless
    }
    if seed is not None:
        options["Seed"] = str(seed)
    stages = []
    for stage, steps, archive in (("Synthesize", 3, "synthesized"),
                                  ("Place", 5, "placed"), ("Route", 3, "routed")):
        stages.append(STAGE_TEMPLATE.format(stage=stage, steps=steps, archive=archive))
        if stage == args.stop_after:
            break
    if args.stop_after == "Bitstream":
        stages.append(BITSTREAM_STAGE)
    transcript = project_dir / "transcript.py"
    transcript.write_text(TRANSCRIPT_TEMPLATE.format(
        project_dir=json.dumps(str(project_dir)),
        rtl_files=json.dumps([str(rtl_dir / path) for path in RTL_FILES], indent=4),
        clock_target=FABRIC_CLOCK_TARGET, clock_name=FABRIC_CLOCK_NAME,
        period=repr(FABRIC_PERIOD_NS), options=json.dumps(options, indent=4),
        stages="\n".join(stages)), encoding="utf-8")

    inputs = [Path(__file__).resolve(), flow, config_hw_src, config_hw_tcl_src,
              config_dir / "hardware.json", transcript]
    inputs += sorted(path for path in rtl_dir.rglob("*") if path.is_file())
    (project_dir / "staging-manifest.json").write_text(json.dumps({
        "config_dir": str(config_dir), "config": config_hw,
        "fabric_clock_mhz": FABRIC_CLOCK_MHZ, "pll": pll_settings,
        "stop_after": args.stop_after, "seed": seed,
        "hardware_preset": args.hardware_preset,
        "sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs},
    }, indent=2) + "\n")

    runtime_log = project_dir / "runtime.log"
    with runtime_log.open("w", encoding="utf-8") as log:
        for line in (
                f"Created project directory: {project_dir}",
                f"Staged RTL directory:      {rtl_dir}",
                f"Generated transcript:      {transcript}",
                f"Hardware config source:    {config_dir}",
                f"CGRA hardware config:      {config_hw['ROWS']} rows x {config_hw['COLS']} cols, "
                f"Y={config_hw['Y_BITS']}, Y_OUT={config_hw['Y_OUT_BITS']}",
                f"Fabric clock constraint:   {FABRIC_CLOCK_MHZ:g} MHz ({FABRIC_PERIOD_NS:g} ns)",
                "Timing-driven place/route: Yes",
                f"Routing effort:            {ROUTING_EFFORT}",
                f"MaxRegisterCount:          {max_register_count}",
                *([f"Seed:                      {seed}"] if seed is not None else []),
                f"Stop after:                {args.stop_after}",
                f"Physical PLL (nominal):    {pll_settings['output_mhz']:g} MHz"):
            write_runtime_line(line, log)
        if args.no_run:
            return 0

        # The vendor run; its output goes to the console, this log and its own.
        command = [args.nxpython, str(transcript)]
        nxpython_log_path = project_dir / "nxpython.log"
        write_runtime_line("Command: " + " ".join(command), log)
        write_runtime_line(f"Runtime log: {log.name}", log)
        write_runtime_line(f"NXpython log: {nxpython_log_path}", log)
        with nxpython_log_path.open("w", encoding="utf-8") as nxpython_log:
            process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
                log.flush()
                nxpython_log.write(line)
                nxpython_log.flush()
            return process.wait()


if __name__ == "__main__":
    sys.exit(main())
