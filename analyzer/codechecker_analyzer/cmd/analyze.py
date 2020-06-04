# -------------------------------------------------------------------------
#
#  Part of the CodeChecker project, under the Apache License v2.0 with
#  LLVM Exceptions. See LICENSE for license information.
#  SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
#
# -------------------------------------------------------------------------
"""
Execute analysis over an already existing build.json compilation database.
"""


import argparse
import collections
import fnmatch
import json
import os
import re
import shutil
import sys

from tu_collector import tu_collector

from codechecker_analyzer import analyzer, analyzer_context, arg, env
from codechecker_analyzer.analyzers import analyzer_types
from codechecker_analyzer.arg import OrderedCheckersAction
from codechecker_analyzer.buildlog import log_parser

from codechecker_common import arg, logger, skiplist_handler
from codechecker_common.util import load_json_or_empty


LOG = logger.get_logger('system')


def get_argparser_ctor_args():
    """
    This method returns a dict containing the kwargs for constructing an
    argparse.ArgumentParser (either directly or as a subparser).
    """

    return {
        'prog': 'CodeChecker analyze',
        'formatter_class': arg.RawDescriptionDefaultHelpFormatter,

        # Description is shown when the command's help is queried directly
        'description': """
Use the previously created JSON Compilation Database to perform an analysis on
the project, outputting analysis results in a machine-readable format.""",

        # Epilogue is shown after the arguments when the help is queried
        # directly.
        'epilog': """
environment variables:
  CC_ANALYZERS_FROM_PATH   Set to `yes` or `1` to enforce taking the analyzers
                           from the `PATH` instead of the given binaries.
  CC_CLANGSA_PLUGIN_DIR    If the CC_ANALYZERS_FROM_PATH environment variable
                           is set you can configure the plugin directory of the
                           Clang Static Analyzer by using this environment
                           variable.


issue hashes:
- By default the issue hash calculation method for 'Clang Static Analyzer' is
context sensitive. It means the hash will be generated based on the following
information:
  * signature of the enclosing function declaration, type declaration or
    namespace.
  * content of the line where the bug is.
  * unique name of the checker.
  * position (column) within the line.

- By default the issue hash calculation method for 'Clang Tidy' is context
insensitive. It means the hash will be generated based on the following
information:
  * 'file name' from the main diag section.
  * 'checker name'.
  * 'checker message'.
  * 'line content' from the source file if can be read up.
  * 'column numbers' from the main diag section.
  * 'range column numbers' only from the control diag sections if column number
    in the range is not the same as the previous control diag section number in
    the bug path. If there are no control sections event section column numbers
    are used.

- context-free: there was a bug and for Clang Tidy the default hash was
generated and not the context free hash (kept for backward compatibility). Use
'context-free-v2' instead of this.

- context-free-v2:
  * 'file name' from the main diag section.
  * 'checker message'.
  * 'line content' from the source file if can be read up. All the whitespaces
    from the source content are removed.
  * 'column numbers' from the main diag sections location.

OUR RECOMMENDATION: we recommend you to use 'context-free-v2' hash because the
hash will not be changed so easily for example on code indentation or when a
checker is renamed.


Compilation databases can be created by instrumenting your project's build via
'CodeChecker log'. To transform the results of the analysis to a human-friendly
format, please see the commands 'CodeChecker parse' or 'CodeChecker store'.""",

        # Help is shown when the "parent" CodeChecker command lists the
        # individual subcommands.
        'help': "Execute the supported code analyzers for the files "
                "recorded in a JSON Compilation Database."
    }


def add_arguments_to_parser(parser):
    """
    Add the subcommand's arguments to the given argparse.ArgumentParser.
    """

    parser.add_argument('logfile',
                        type=str,
                        help="Path to the JSON compilation command database "
                             "files which were created during the build. "
                             "The analyzers will check only the files "
                             "registered in these build databases.")

    parser.add_argument('-j', '--jobs',
                        type=int,
                        dest="jobs",
                        required=False,
                        default=1,
                        help="Number of threads to use in analysis. More "
                             "threads mean faster analysis at the cost of "
                             "using more memory.")

    skip_mode = parser.add_mutually_exclusive_group()
    skip_mode.add_argument('-i', '--ignore', '--skip',
                           dest="skipfile",
                           required=False,
                           default=argparse.SUPPRESS,
                           help="Path to the Skipfile dictating which project "
                                "files should be omitted from analysis. "
                                "Please consult the User guide on how a "
                                "Skipfile should be laid out.")

    skip_mode.add_argument('--file',
                           nargs='+',
                           dest="files",
                           metavar='FILE',
                           required=False,
                           default=argparse.SUPPRESS,
                           help="Analyze only the given file(s) not the whole "
                                "compilation database. Absolute directory "
                                "paths should start with '/', relative "
                                "directory paths should start with '*' and "
                                "it can contain path glob pattern. "
                                "Example: '/path/to/main.cpp', 'lib/*.cpp', "
                                "*/test*'.")

    parser.add_argument('-o', '--output',
                        dest="output_path",
                        required=True,
                        default=argparse.SUPPRESS,
                        help="Store the analysis output in the given folder.")

    parser.add_argument('--compiler-info-file',
                        dest="compiler_info_file",
                        required=False,
                        default=argparse.SUPPRESS,
                        help="Read the compiler includes and target from the "
                             "specified file rather than invoke the compiler "
                             "executable.")

    parser.add_argument('--keep-gcc-include-fixed',
                        dest="keep_gcc_include_fixed",
                        required=False,
                        action='store_true',
                        default=False,
                        help="There are some implicit include paths which are "
                             "only used by GCC (include-fixed). This flag "
                             "determines whether these should be kept among "
                             "the implicit include paths.")

    parser.add_argument('--keep-gcc-intrin',
                        dest="keep_gcc_intrin",
                        required=False,
                        action='store_true',
                        default=False,
                        help="There are some implicit include paths which "
                             "contain GCC-specific header files (those "
                             "which end with intrin.h). This flag determines "
                             "whether these should be kept among the implicit "
                             "include paths. Use this flag if Clang analysis "
                             "fails with error message related to __builtin "
                             "symbols.")

    parser.add_argument('-t', '--type', '--output-format',
                        dest="output_format",
                        required=False,
                        choices=['plist'],
                        default='plist',
                        help="Specify the format the analysis results should "
                             "use.")

    parser.add_argument('-q', '--quiet',
                        dest="quiet",
                        action='store_true',
                        default=argparse.SUPPRESS,
                        required=False,
                        help="Do not print the output or error of the "
                             "analyzers to the standard output of "
                             "CodeChecker.")

    parser.add_argument('-c', '--clean',
                        dest="clean",
                        required=False,
                        action='store_true',
                        default=argparse.SUPPRESS,
                        help="Delete analysis reports stored in the output "
                             "directory. (By default, CodeChecker would keep "
                             "reports and overwrites only those files that "
                             "were update by the current build command).")

    parser.add_argument('--compile-uniqueing',
                        type=str,
                        dest="compile_uniqueing",
                        default="none",
                        required=False,
                        help="Specify the method the compilation "
                             "actions in the  compilation database are "
                             "uniqued before analysis. "
                             "CTU analysis works properly only if "
                             "there is exactly one "
                             "compilation action per source file. "
                             "none(default in non CTU mode): "
                             "no uniqueing is done. "
                             "strict: no uniqueing is done, "
                             "and an error is given if "
                             "there is more than one compilation "
                             "action for a source file. "
                             "alpha(default in CTU mode): If there is more "
                             "than one compilation action for a source "
                             "file, only the one is kept that belongs to the "
                             "alphabetically first "
                             "compilation target. "
                             "If none of the above given, "
                             "this parameter should "
                             "be a python regular expression."
                             "If there is more than one compilation action "
                             "for a source, "
                             "only the one is kept which matches the "
                             "given python regex. If more than one "
                             "matches an error is given. "
                             "The whole compilation "
                             "action text is searched for match.")

    parser.add_argument('--report-hash',
                        dest="report_hash",
                        default=argparse.SUPPRESS,
                        required=False,
                        choices=['context-free', 'context-free-v2'],
                        help="R|Specify the hash calculation method for "
                             "reports. By default the calculation method for "
                             "Clang Static Analyzer is context sensitive and "
                             "for Clang Tidy it is context insensitive.\n"
                             "You can use the following calculation methods:\n"
                             "- context-free: there was a bug and for Clang "
                             "Tidy not the context free hash was generated "
                             "(kept for backward compatibility).\n"
                             "- context-free-v2: context free hash is used "
                             "for ClangSA and Clang Tidy.\n"
                             "See the 'issue hashes' section of the help "
                             "message of this command below for more "
                             "information.\n"
                             "USE WISELY AND AT YOUR OWN RISK!")

    parser.add_argument('-n', '--name',
                        dest="name",
                        required=False,
                        default=argparse.SUPPRESS,
                        help="Annotate the run analysis with a custom name in "
                             "the created metadata file.")

    analyzer_opts = parser.add_argument_group("analyzer arguments")

    analyzer_opts.add_argument('--analyzers',
                               nargs='+',
                               dest='analyzers',
                               metavar='ANALYZER',
                               required=False,
                               choices=analyzer_types.supported_analyzers,
                               default=argparse.SUPPRESS,
                               help="Run analysis only with the analyzers "
                                    "specified. Currently supported analyzers "
                                    "are: " +
                                    ', '.join(analyzer_types.
                                              supported_analyzers) + ".")

    analyzer_opts.add_argument('--capture-analysis-output',
                               dest='capture_analysis_output',
                               action='store_true',
                               default=argparse.SUPPRESS,
                               required=False,
                               help="Store standard output and standard error "
                                    "of successful analyzer invocations "
                                    "into the '<OUTPUT_DIR>/success' "
                                    "directory.")

    analyzer_opts.add_argument('--config',
                               dest='config_file',
                               required=False,
                               help="Allow the configuration from an explicit "
                                    "JSON based configuration file. The "
                                    "value of the 'analyzer' key in the "
                                    "config file will be emplaced as command "
                                    "line arguments. The format of "
                                    "configuration file is: "
                                    "{"
                                    "  \"analyzer\": ["
                                    "    \"--enable=core.DivideZero\","
                                    "    \"--enable=core.CallAndMessage\","
                                    "    \"--clean\""
                                    "  ]"
                                    "}.")

    analyzer_opts.add_argument('--saargs',
                               dest="clangsa_args_cfg_file",
                               required=False,
                               default=argparse.SUPPRESS,
                               help="File containing argument which will be "
                                    "forwarded verbatim for the Clang Static "
                                    "Analyzer.")

    analyzer_opts.add_argument('--tidyargs',
                               dest="tidy_args_cfg_file",
                               required=False,
                               default=argparse.SUPPRESS,
                               help="File containing argument which will be "
                                    "forwarded verbatim for Clang-Tidy.")

    analyzer_opts.add_argument('--tidy-config',
                               dest='tidy_config',
                               required=False,
                               default=argparse.SUPPRESS,
                               help="A file in YAML format containing the "
                                    "configuration of clang-tidy checkers. "
                                    "The file can be dumped by "
                                    "'CodeChecker analyzers --dump-config "
                                    "clang-tidy' command.")

    analyzer_opts.add_argument('--analyzer-config',
                               dest='analyzer_config',
                               nargs='*',
                               default=["clang-tidy:HeaderFilterRegex=.*"],
                               help="Analyzer configuration options in the "
                                    "following format: analyzer:key=value. "
                                    "The collection of the options can be "
                                    "printed with "
                                    "'CodeChecker analyzers "
                                    "--analyzer-config'. To disable the "
                                    "default behaviour of this option you can "
                                    "use the "
                                    "'clang-tidy:take-config-from-directory="
                                    "true' option.")

    analyzer_opts.add_argument('--checker-config',
                               dest='checker_config',
                               nargs='*',
                               default=argparse.SUPPRESS,
                               help="Checker configuration options in the "
                                    "following format: analyzer:key=value. "
                                    "The collection of the options can be "
                                    "printed with "
                                    "'CodeChecker checkers --checker-config'.")

    analyzer_opts.add_argument('--timeout',
                               type=int,
                               dest='timeout',
                               required=False,
                               default=argparse.SUPPRESS,
                               help="The amount of time (in seconds) that "
                                    "each analyzer can spend, individually, "
                                    "to analyze the project. If the analysis "
                                    "of a particular file takes longer than "
                                    "this time, the analyzer is killed and "
                                    "the analysis is considered as a failed "
                                    "one.")

    context = analyzer_context.get_context()
    clang_has_z3 = analyzer_types.is_z3_capable(context)

    if clang_has_z3:
        analyzer_opts.add_argument('--z3',
                                   dest='enable_z3',
                                   choices=['on', 'off'],
                                   default='off',
                                   help="Enable the z3 solver backend. This "
                                        "allows reasoning over more complex "
                                        "queries, but performance is worse "
                                        "than the default range-based "
                                        "constraint solver.")

    clang_has_z3_refutation = analyzer_types.is_z3_refutation_capable(context)

    if clang_has_z3_refutation:
        analyzer_opts.add_argument('--z3-refutation',
                                   dest='enable_z3_refutation',
                                   choices=['on', 'off'],
                                   default='on' if clang_has_z3_refutation
                                   else 'off',
                                   help="Switch on/off the Z3 SMT Solver "
                                        "backend to "
                                        "reduce false positives. The results "
                                        "of the ranged based constraint "
                                        "solver in the Clang Static Analyzer "
                                        "will be cross checked with the Z3 "
                                        "SMT solver. This should not cause "
                                        "that much of a slowdown compared to "
                                        "using the Z3 solver only.")

    if analyzer_types.is_ctu_capable(context):
        ctu_opts = parser.add_argument_group(
            "cross translation unit analysis arguments",
            """
These arguments are only available if the Clang Static Analyzer supports
Cross-TU analysis. By default, no CTU analysis is run when
'CodeChecker analyze' is called.""")

        ctu_modes = ctu_opts.add_mutually_exclusive_group()
        ctu_modes.add_argument('--ctu', '--ctu-all',
                               action='store_const',
                               const=[True, True],
                               dest='ctu_phases',
                               default=argparse.SUPPRESS,
                               help="Perform Cross Translation Unit (CTU) "
                                    "analysis, both 'collect' and 'analyze' "
                                    "phases. In this mode, the extra files "
                                    "created by 'collect' are cleaned up "
                                    "after the analysis.")

        ctu_modes.add_argument('--ctu-collect',
                               action='store_const',
                               const=[True, False],
                               dest='ctu_phases',
                               default=argparse.SUPPRESS,
                               help="Perform the first, 'collect' phase of "
                                    "Cross-TU analysis. This phase generates "
                                    "extra files needed by CTU analysis, and "
                                    "puts them into '<OUTPUT_DIR>/ctu-dir'. "
                                    "NOTE: If this argument is present, "
                                    "CodeChecker will NOT execute the "
                                    "analyzers!")

        ctu_modes.add_argument('--ctu-analyze',
                               action='store_const',
                               const=[False, True],
                               dest='ctu_phases',
                               default=argparse.SUPPRESS,
                               help="Perform the second, 'analyze' phase of "
                                    "Cross-TU analysis, using already "
                                    "available extra files in "
                                    "'<OUTPUT_DIR>/ctu-dir'. (These files "
                                    "will not be cleaned up in this mode.)")

        ctu_opts.add_argument('--ctu-reanalyze-on-failure',
                              action='store_true',
                              dest='ctu_reanalyze_on_failure',
                              default=argparse.SUPPRESS,
                              help="If Cross-TU analysis is enabled and fails "
                                   "for some reason, try to re analyze the "
                                   "same translation unit without "
                                   "Cross-TU enabled.")

    if analyzer_types.is_statistics_capable(context):
        stat_opts = parser.add_argument_group(
            "Statistics analysis feature arguments",
            """
These arguments are only available if the Clang Static Analyzer supports
Statistics-based analysis (e.g. statisticsCollector.ReturnValueCheck,
statisticsCollector.SpecialReturnValue checkers are available).""")

        stat_opts.add_argument('--stats-collect', '--stats-collect',
                               action='store',
                               default=argparse.SUPPRESS,
                               dest='stats_output',
                               help="Perform the first, 'collect' phase of "
                                    "Statistical analysis. This phase "
                                    "generates extra files needed by "
                                    "statistics analysis, and "
                                    "puts them into "
                                    "'<STATS_OUTPUT>'."
                                    " NOTE: If this argument is present, "
                                    "CodeChecker will NOT execute the "
                                    "analyzers!")

        stat_opts.add_argument('--stats-use', '--stats-use',
                               action='store',
                               default=argparse.SUPPRESS,
                               dest='stats_dir',
                               help="Use the previously generated statistics "
                                    "results for the analysis from the given "
                                    "'<STATS_DIR>'.")

        stat_opts.add_argument('--stats',
                               action='store_true',
                               default=argparse.SUPPRESS,
                               dest='stats_enabled',
                               help="Perform both phases of "
                                    "Statistical analysis. This phase "
                                    "generates extra files needed by "
                                    "statistics analysis and enables "
                                    "the statistical checkers. "
                                    "No need to enable them explicitly.")

        stat_opts.add_argument('--stats-min-sample-count',
                               action='store',
                               default="10",
                               type=int,
                               dest='stats_min_sample_count',
                               help="Minimum number of samples (function call"
                                    " occurrences) to be collected"
                                    " for a statistics to be relevant "
                                    "'<MIN-SAMPLE-COUNT>'.")

        stat_opts.add_argument('--stats-relevance-threshold',
                               action='store',
                               default="0.85",
                               type=float,
                               dest='stats_relevance_threshold',
                               help="The minimum ratio of calls of function "
                                    "f that must have a certain property "
                                    "property to consider it true for that "
                                    "function (calculated as calls "
                                    "with a property/all calls)."
                                    " CodeChecker will warn for"
                                    " calls of f do not have that property."
                                    "'<RELEVANCE_THRESHOLD>'.")

    checkers_opts = parser.add_argument_group(
        "checker configuration",
        """
Checkers
------------------------------------------------
The analyzer performs checks that are categorized into families or "checkers".
See 'CodeChecker checkers' for the list of available checkers. You can
fine-tune which checkers to use in the analysis by setting the enabled and
disabled flags starting from the bigger groups and going inwards, e.g.
'-e core -d core.uninitialized -e core.uninitialized.Assign' will enable every
'core' checker, but only 'core.uninitialized.Assign' from the
'core.uninitialized' group. Please consult the manual for details. Disabling
certain checkers - such as the 'core' group - is unsupported by the LLVM/Clang
community, and thus discouraged.

Compiler warnings and errors
------------------------------------------------
Compiler warnings are diagnostic messages that report constructions that are
not inherently erroneous but that are risky or suggest there may have been an
error. Compiler warnings are named 'clang-diagnostic-<warning-option>', e.g.
Clang warning controlled by '-Wliteral-conversion' will be reported with check
name 'clang-diagnostic-literal-conversion'. You can fine-tune which warnings to
use in the analysis by setting the enabled and disabled flags starting from the
bigger groups and going inwards, e.g. '-e Wunused -d Wno-unused-parameter' will
enable every 'unused' warnings except 'unused-parameter'. These flags should
start with a capital 'W' or 'Wno-' prefix followed by the waning name (E.g.:
'-e Wliteral-conversion', '-d Wno-literal-conversion'). By default '-Wall' and
'-Wextra' warnings are enabled. For more information see:
https://clang.llvm.org/docs/DiagnosticsReference.html.
Sometimes GCC is more permissive than Clang, so it is possible that a specific
construction doesn't compile with Clang but compiles with GCC. These
compiler errors are also collected as CodeChecker reports as
'clang-diagnostic-error'.
Note that compiler errors and warnings are captured by CodeChecker only if it
was emitted by clang-tidy.""")

    checkers_opts.add_argument('-e', '--enable',
                               dest="enable",
                               metavar='checker/group/profile',
                               default=argparse.SUPPRESS,
                               action=OrderedCheckersAction,
                               help="Set a checker (or checker group) "
                                    "to BE USED in the analysis.")

    checkers_opts.add_argument('-d', '--disable',
                               dest="disable",
                               metavar='checker/group/profile',
                               default=argparse.SUPPRESS,
                               action=OrderedCheckersAction,
                               help="Set a checker (or checker group) "
                                    "to BE PROHIBITED from use in the "
                                    "analysis.")

    checkers_opts.add_argument('--enable-all',
                               dest="enable_all",
                               action='store_true',
                               required=False,
                               default=argparse.SUPPRESS,
                               help="Force the running analyzers to use "
                                    "almost every checker available. The "
                                    "checker groups 'alpha.', 'debug.' and "
                                    "'osx.' (on Linux) are NOT enabled "
                                    "automatically and must be EXPLICITLY "
                                    "specified. WARNING! Enabling all "
                                    "checkers might result in the analysis "
                                    "losing precision and stability, and "
                                    "could even result in a total failure of "
                                    "the analysis. USE WISELY AND AT YOUR "
                                    "OWN RISK!")

    logger.add_verbose_arguments(parser)
    parser.set_defaults(func=main,
                        func_process_config_file=process_config_file)


def process_config_file(args):
    """
    Handler to get config file options.
    """
    if args.config_file and os.path.exists(args.config_file):
        cfg = load_json_or_empty(args.config_file, default={})
        return cfg.get('analyzer', [])


def check_config_file(args):
    """
    LOG and check about the config file usage.

    If a config file is set but does not exist the program will
    exit.
    LOG is not initialized in the process_config_file function yet
    so we can not log the usage there. Using print will
    always print out the config file data which can mess up the
    tests depending on the output.
    """

    if args.config_file and not os.path.exists(args.config_file):
        LOG.error("Configuration file '%s' does not exist.",
                  args.config_file)
        sys.exit(1)
    elif not args.config_file:
        return

    cfg = load_json_or_empty(args.config_file, default={})
    if cfg.get("enabled"):
        LOG.debug("Using config file: '%s'.", args.config_file)
        return cfg.get('analyzer', [])

    LOG.debug("Config file '%s' is available but disabled.", args.config_file)


def __get_source_dependencies(compile_commands):
    """ Returns a set of dependencies for each files in each translation unit.
    """
    dependencies = collections.defaultdict(set)
    for build_action in compile_commands:
        files, _ = tu_collector.get_dependent_headers(
            build_action['command'],
            build_action['directory'])

        source_file = os.path.join(build_action['directory'],
                                   build_action['file'])
        for f in files:
            dependencies[f].add(source_file)

    return dependencies


def __get_skip_handler(args, compile_commands):
    """
    Initialize and return a skiplist handler if
    there is a skip list file in the arguments or files options is provided.
    """
    skip_file_content = ""

    if 'files' in args:
        # Creates a skip file where all source files will be skipped except
        # the given source files.
        skip_files = ['+{0}'.format(f) for f in args.files]
        skip_files.append('-*')

        skip_file_content = "\n".join(skip_files)
    elif 'skipfile' in args:
        with open(args.skipfile,
                  encoding="utf-8", errors="ignore") as skip_file:
            skip_file_content = skip_file.read()

    if skip_file_content:
        LOG.debug_analyzer("Creating skiplist handler.")
        handler = skiplist_handler.SkipListHandler(skip_file_content)

        analyze_headers = []

        # Check whether the skip file contains a header file which is not
        # skipped.
        for skip_line in handler.skip_file_lines:
            if skip_line[0] == '+' and \
              skip_line.lower().endswith((".h", ".hh", ".hpp")):

                norm_skip_path = os.path.normpath(skip_line[1:].strip())
                rexpr = re.compile(
                    fnmatch.translate(norm_skip_path + '*'))
                analyze_headers.append((skip_line, rexpr))

        # Get source files which depend on the previously collected header
        # files and create a new skip list handler where we include these
        # files in the beginning.
        if analyze_headers:
            LOG.info("Get source files which depend on some header files and "
                     "should be analyzed by your skip file.")

            dependencies = __get_source_dependencies(compile_commands)

            analyze_header_deps = []
            for f, deps in dependencies.items():
                for _, rexpr in analyze_headers:
                    if rexpr.match(f):
                        analyze_header_deps.extend(["+" + d for d in deps])

            if analyze_header_deps:
                LOG.info("Your skip file contained some header files (%s) to "
                         "be analyzed. Analysis can not be executed on header "
                         "files only. For this reason CodeChecker will "
                         "analyze the following source files which include "
                         "the header files:\n%s",
                         ', '.join([f for (f, _) in analyze_headers]),
                         '\n'.join([" " + f for f in analyze_header_deps]))

                skip_file_content = \
                    "\n".join(analyze_header_deps) + "\n" + skip_file_content

                return skiplist_handler.SkipListHandler(skip_file_content)

        return handler


def __update_skip_file(args):
    """
    Remove previous skip file if there was any.
    """
    skip_file_to_send = os.path.join(args.output_path, 'skip_file')

    if os.path.exists(skip_file_to_send):
        os.remove(skip_file_to_send)

    if 'skipfile' in args:
        LOG.debug("Copying skip file %s to %s",
                  args.skipfile, skip_file_to_send)
        shutil.copyfile(args.skipfile, skip_file_to_send)


def __cleanup_metadata(metadata_prev, metadata):
    """ Cleanup metadata.

    If the source file for a plist does not exist, remove the plist from the
    system and from the metadata.
    """
    if not metadata_prev:
        return

    result_src_files = __get_result_source_files(metadata_prev)
    for plist_file, source_file in result_src_files.items():
        if not os.path.exists(source_file):
            try:
                LOG.info("Remove plist file '%s' because it refers to a "
                         "source file ('%s') which was removed.",
                         plist_file, source_file)
                del metadata['result_source_files'][plist_file]
                os.remove(plist_file)
            except OSError:
                LOG.warning("Failed to remove plist file: %s", plist_file)


def __get_result_source_files(metadata):
    """ Get result source files from the given metadata. """
    if 'result_source_files' in metadata:
        return metadata['result_source_files']

    result_src_files = {}
    for tool in metadata.get('tools', {}):
        r_src_files = tool.get('result_source_files', {})
        result_src_files.update(r_src_files.items())

    return result_src_files


def main(args):
    """
    Perform analysis on the given logfiles and store the results in a machine-
    readable format.
    """
    logger.setup_logger(args.verbose if 'verbose' in args else None)

    check_config_file(args)

    if not os.path.exists(args.logfile):
        LOG.error("The specified logfile '%s' does not exist!", args.logfile)
        sys.exit(1)

    args.output_path = os.path.abspath(args.output_path)
    if os.path.exists(args.output_path) and \
            not os.path.isdir(args.output_path):
        LOG.error("The given output path is not a directory: " +
                  args.output_path)
        sys.exit(1)

    if 'enable_all' in args:
        LOG.info("'--enable-all' was supplied for this analysis.")

    # We clear the output directory in the following cases.
    ctu_dir = os.path.join(args.output_path, 'ctu-dir')
    if 'ctu_phases' in args and args.ctu_phases[0] and \
            os.path.isdir(ctu_dir):
        # Clear the CTU-dir if the user turned on the collection phase.
        LOG.debug("Previous CTU contents have been deleted.")
        shutil.rmtree(ctu_dir)

    if 'clean' in args and os.path.isdir(args.output_path):
        LOG.info("Previous analysis results in '%s' have been removed, "
                 "overwriting with current result", args.output_path)
        shutil.rmtree(args.output_path)

    if not os.path.exists(args.output_path):
        os.makedirs(args.output_path)

    LOG.debug("args: " + str(args))
    LOG.debug("Output will be stored to: '" + args.output_path + "'")

    config_option_re = re.compile(r'^({}):.+=.+$'.format(
        '|'.join(analyzer_types.supported_analyzers)))

    # Check the format of analyzer options.
    if 'analyzer_config' in args:
        for config in args.analyzer_config:
            if not re.match(config_option_re, config):
                LOG.error("Analyzer option in wrong format: %s", config)
                sys.exit(1)

    # Check the format of checker options.
    if 'checker_config' in args:
        for config in args.checker_config:
            if not re.match(config_option_re, config):
                LOG.error("Checker option in wrong format: %s", config)
                sys.exit(1)

    compile_commands = load_json_or_empty(args.logfile, default={})

    # Process the skip list if present.
    skip_handler = __get_skip_handler(args, compile_commands)

    # Enable alpha uniqueing by default if ctu analysis is used.
    if 'none' in args.compile_uniqueing and 'ctu_phases' in args:
        args.compile_uniqueing = "alpha"

    compiler_info_file = None
    if 'compiler_info_file' in args:
        LOG.debug("Compiler info is read from: %s", args.compiler_info_file)
        if not os.path.exists(args.compiler_info_file):
            LOG.error("Compiler info file %s does not exist",
                      args.compiler_info_file)
            sys.exit(1)
        compiler_info_file = args.compiler_info_file

    ctu_or_stats_enabled = False
    # Skip list is applied only in pre-analysis
    # if --ctu-collect was called explicitly.
    pre_analysis_skip_handler = None
    if 'ctu_phases' in args:
        ctu_collect = args.ctu_phases[0]
        ctu_analyze = args.ctu_phases[1]
        if ctu_collect and not ctu_analyze:
            pre_analysis_skip_handler = skip_handler

        if ctu_collect or ctu_analyze:
            ctu_or_stats_enabled = True

    # Skip list is applied only in pre-analysis
    # if --stats-collect was called explicitly.
    if 'stats_output' in args and args.stats_output:
        pre_analysis_skip_handler = skip_handler
        ctu_or_stats_enabled = True

    if 'stats_enabled' in args and args.stats_enabled:
        ctu_or_stats_enabled = True

    context = analyzer_context.get_context()
    analyzer_env = env.extend(context.path_env_extra,
                              context.ld_lib_path_extra)

    # Number of all the compilation commands in the parsed log files,
    # logged by the logger.
    all_cmp_cmd_count = len(compile_commands)

    actions, skipped_cmp_cmd_count = log_parser.parse_unique_log(
        compile_commands,
        args.output_path,
        args.compile_uniqueing,
        compiler_info_file,
        args.keep_gcc_include_fixed,
        args.keep_gcc_intrin,
        skip_handler,
        pre_analysis_skip_handler,
        ctu_or_stats_enabled,
        analyzer_env)

    if not actions:
        LOG.info("No analysis is required.\nThere were no compilation "
                 "commands in the provided compilation database or "
                 "all of them were skipped.")
        sys.exit(0)

    uniqued_compilation_db_file = os.path.join(
        args.output_path, "unique_compile_commands.json")
    with open(uniqued_compilation_db_file, 'w',
              encoding="utf-8", errors="ignore") as f:
        json.dump(actions, f,
                  cls=log_parser.CompileCommandEncoder)

    metadata = {
        'version': 2,
        'tools': [{
            'name': 'codechecker',
            'action_num': len(actions),
            'command': sys.argv,
            'version': "{0} ({1})".format(context.package_git_tag,
                                          context.package_git_hash),
            'working_directory': os.getcwd(),
            'output_path': args.output_path,
            'result_source_files': {},
            'analyzers': {}
        }]}
    metadata_tool = metadata['tools'][0]

    if 'name' in args:
        metadata_tool['run_name'] = args.name

    # Update metadata dictionary with old values.
    metadata_file = os.path.join(args.output_path, 'metadata.json')
    metadata_prev = None
    if os.path.exists(metadata_file):
        metadata_prev = load_json_or_empty(metadata_file)
        metadata_tool['result_source_files'] = \
            __get_result_source_files(metadata_prev)

    CompileCmdParseCount = \
        collections.namedtuple('CompileCmdParseCount',
                               'total, analyze, skipped, removed_by_uniqueing')
    cmp_cmd_to_be_uniqued = all_cmp_cmd_count - skipped_cmp_cmd_count

    # Number of compile commands removed during uniqueing.
    removed_during_uniqueing = cmp_cmd_to_be_uniqued - len(actions)

    all_to_be_analyzed = cmp_cmd_to_be_uniqued - removed_during_uniqueing

    compile_cmd_count = CompileCmdParseCount(
        total=all_cmp_cmd_count,
        analyze=all_to_be_analyzed,
        skipped=skipped_cmp_cmd_count,
        removed_by_uniqueing=removed_during_uniqueing)

    LOG.debug_analyzer("Total number of compile commands without "
                       "skipping or uniqueing: %d", compile_cmd_count.total)
    LOG.debug_analyzer("Compile commands removed by uniqueing: %d",
                       compile_cmd_count.removed_by_uniqueing)
    LOG.debug_analyzer("Compile commands skipped during log processing: %d",
                       compile_cmd_count.skipped)
    LOG.debug_analyzer("Compile commands forwarded for analysis: %d",
                       compile_cmd_count.analyze)

    analyzer.perform_analysis(args, skip_handler, context, actions,
                              metadata_tool,
                              compile_cmd_count)

    __update_skip_file(args)
    __cleanup_metadata(metadata_prev, metadata)

    LOG.debug("Analysis metadata write to '%s'", metadata_file)
    with open(metadata_file, 'w',
              encoding="utf-8", errors="ignore") as metafile:
        json.dump(metadata, metafile)

    # WARN: store command will search for this file!!!!
    compile_cmd_json = os.path.join(args.output_path, 'compile_cmd.json')
    try:
        source = os.path.abspath(args.logfile)
        target = os.path.abspath(compile_cmd_json)

        if source != target:
            shutil.copyfile(source, target)
    except shutil.Error:
        LOG.debug("Compilation database JSON file is the same.")
    except Exception:
        LOG.debug("Copying compilation database JSON file failed.")

    try:
        # pylint: disable=no-name-in-module
        from codechecker_analyzer import analyzer_statistics
        analyzer_statistics.collect(metadata, "analyze")
    except Exception:
        pass
