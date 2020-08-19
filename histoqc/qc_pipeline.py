import logging
import os, time
import errno
import glob
import argparse
import configparser
import shutil
import matplotlib as mpl  # need to do this before anything else tries to access
import multiprocessing, logging
from importlib import import_module
import warnings
import histoqc
from histoqc.BaseImage import BaseImage
import sys
import datetime

# --- setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

file = logging.FileHandler(filename="error.log")
file.setLevel(logging.WARNING)
file.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logging.getLogger('').addHandler(file)

# --- setup plotting backend
if os.name != "nt" and os.environ.get('DISPLAY', '') == '':
    logging.info('no display found. Using non-interactive Agg backend')
    mpl.use('Agg')
else:
    mpl.use('TkAgg')

import matplotlib.pyplot as plt

warnings.filterwarnings("ignore")

# ---Setup globals for output
batch = 1
nfiledone = 0
csv_report = None
first = True
failed = []
headers = []


# --- setup worker functions
def worker(i, nfiles, fname, args, lconfig, process_queue, lock, shared_dict):
    fname_outdir = args.outdir + os.sep + os.path.basename(fname)
    # fname_outdir = fname_outdir.replace("lnk.", "")
    if os.path.isdir(fname_outdir):  # directory exists
        if args.force:
            # remove entire directory to ensure no old files are present
            shutil.rmtree(fname_outdir)
        else:
            # otherwise skip it
            logging.warning(
                f"{fname} already seems to be processed "
                f"(output directory exists), skipping. To avoid this "
                f"behavior use --force")
            return
    make_dir_safe(fname_outdir)

    logging.info(f"-----Working on:\t{fname}\t\t{i+1} of {nfiles}")
    try:
        s = BaseImage(fname, fname_outdir,
                      dict(lconfig.items("BaseImage.BaseImage")))

        for process, process_params in process_queue:
            process_params["lock"] = lock
            process_params["shared_dict"] = shared_dict
            process(s, process_params)
            s["completed"].append(process.__name__)
    except Exception as e:
        e.args += (fname, str(e.__traceback__.tb_next.tb_frame.f_code))
        raise e

    # need to get rid of handle because it can't be pickled
    s.pop("os_handle", None)
    return s


def worker_error(e):
    fname = e.args[1]
    # func = e.args[2]
    func = ""
    err_string = " ".join((str(e.__class__), e.__doc__, str(e), func))
    #err_string = err_string.replace("\n", " ")
    logging.error(f"{fname} - \t{func} - Error analyzing file (skipping): "
                  f"\t {err_string}")
    failed.append((fname, err_string))


def load_pipeline(lconfig):
    queue = []
    in_main = multiprocessing.current_process()._identity == ()
    if in_main:
        logging.info("Pipeline will use these steps:")
    steps = lconfig.get('pipeline', 'steps').splitlines()
    headers.append("pipeline: "+" ".join(steps))

    for process in steps:
        mod_name, func_name = process.split('.')
        if in_main:
            logging.info(f"\t\t{mod_name}\t{func_name}")
        mod = import_module(f'histoqc.{mod_name}')
        func_name = func_name.split(":")[0]  # take base of function name
        func = getattr(mod, func_name)

        if lconfig.has_section(process):
            params = dict(lconfig.items(process))
        else:
            params = {}

        queue.append((func, params))
    return queue


def make_dir_safe(path):
    try:
        os.makedirs(path)
    except OSError as exception:
        if exception.errno != errno.EEXIST:
            raise


def main():
    manager = multiprocessing.Manager()
    lock = manager.Lock()
    shared_dict = manager.dict()
    headers.append(f"start_time:\t{datetime.datetime.now()}")
    parser = argparse.ArgumentParser(description='')

    default_outdir = f"./logs/histoqc_output_{time.strftime('%Y%m%d-%H%M%S')}"
    default_config = f"{histoqc.__path__[0]}/config.ini"

    parser.add_argument('input_pattern',
                        help="input filename pattern (try: *.svs or target_path/*.svs ), or tsv file containing list of files to analyze",
                        nargs="*")
    parser.add_argument('-o', '--outdir', help="outputdir, default ./histoqc_output", default=default_outdir, type=str)
    parser.add_argument('-p', '--basepath',
                        help="base path to add to file names, helps when producing data using existing output file as input",
                        default="", type=str)
    parser.add_argument('-c', '--config', help="config file to use", default=default_config, type=str)
    parser.add_argument('-f', '--force', help="force overwriting of existing files", action="store_true")
    parser.add_argument('-b', '--batch', help="break results file into subsets of this size", type=int,
                        default=float("inf"))
    parser.add_argument('-n', '--nthreads', help="number of threads to launch", type=int, default=1)
    parser.add_argument('-s', '--symlinkoff', help="turn OFF symlink creation", action="store_true")

    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)

    args = parser.parse_args()

    config = configparser.ConfigParser()

    if args.config is None:
        logging.warning(f"Configuration file not set (--config), "
                        f"using default: {args.config}")

    config.read(args.config)

    process_queue = load_pipeline(config)

    # start worker processes
    pool = multiprocessing.Pool(
        processes=args.nthreads, initializer=load_pipeline, initargs=(config,))
    logging.info("----------")
    # make output directory and create report file
    make_dir_safe(args.outdir)
    headers.append(f"outdir:\t{os.path.realpath(args.outdir)}")
    headers.append(f"config_file:\t{os.path.realpath(args.config)}")
    headers.append(f"command_line_args:\t{' '.join(sys.argv)}")

    if len(glob.glob(args.outdir + os.sep + "results*.tsv")) > 0:
        if args.force:
            logging.info("Previous run detected....overwriting (--force set)")
            overwrite_flag = "w"
        else:
            logging.info(
                "Previous run detected....skipping completed (--force not set)")
            overwrite_flag = "a"
    else:
        overwrite_flag = "w"

    if args.batch != float("inf"):
        filename = f"{args.outdir}{os.sep}results_{batch}.tsv"
    else:
        filename = f"{args.outdir}{os.sep}results.tsv"
    csv_report = open(filename, overwrite_flag, buffering=1)

    # get list of files, there are 3 options:
    files = []
    # if the user supplied a different basepath, make sure it ends with an os.sep
    basepath = args.basepath + os.sep if len(args.basepath) > 0 else ""

    if not args.input_pattern:
        logging.error("No imput files given")
        sys.exit(1)

    if len(args.input_pattern) > 1:  # bash has sent us a list of files
        files = args.input_pattern
    elif args.input_pattern[0].endswith("tsv"):  # user sent us an input file
        # load first column here and store into files
        with open(args.input_pattern[0], 'r') as f:
            for line in f:
                if line[0] == "#":
                    continue
                files.append(basepath + line.strip().split("\t")[0])
    else:  # user sent us a wildcard, need to use glob to find files
        files = glob.glob(args.basepath + args.input_pattern[0])

    n = len(files)
    logging.info(f"Number of files detected by pattern:\t{n}")

    def callback(s):
        if s is None:
            return

        # This is such a freaking hack
        global nfiledone, batch, first
        nonlocal csv_report

        if nfiledone and nfiledone % args.batch == 0:
            csv_report.close()
            batch += 1
            filename = f"{args.outdir}{os.sep}results_{batch}.tsv"
            csv_report = open(filename, overwrite_flag, buffering=1)
            first = True

        # add headers to output file, don't do this if we're in append mode
        if first and overwrite_flag == "w":
            first = False
            csv_report.write("\n".join(["#" + s for s in headers])+"\n")
            # always add warnings field last
            csv_report.write("#dataset:"+"\t".join(s["output"])+"\twarnings\n")

        csv_report.write(
            "\t".join([str(s[field]) for field in s["output"]]) +
            "\t" + "|".join(s["warnings"]) + "\n")

        csv_report.flush()
        nfiledone += 1

    # now do analysis of files
    results = []
    for i, fname in enumerate(files):
        fname = os.path.realpath(fname)
        res = pool.apply_async(
            worker,
            args=(i, n, fname, args, config, process_queue, lock, shared_dict),
            callback=callback,
            #error_callback=worker_error
        )
        results.append(res)

    for r in results:
        logging.info(r.get(timeout=60*30)) # 30 mins??

    pool.close()
    pool.join()

    csv_report.close()

    logging.info("------------Done---------\n")

    if failed:
        logging.info("These images failed (available also in error.log), "
                     "warnings are listed in warnings column in output:")

        for fname, error in failed:
            logging.info(f"{fname}\t{error}")

    if not args.symlinkoff:
        origin = os.path.realpath(args.outdir)
        #target = os.path.normpath(
        #    histoqc.__path__[0] + "/UserInterface/Data/" +
        #    os.path.basename(os.path.normpath(args.outdir)))
        data_dir = "/app/data/ui/data/"
        make_dir_safe(data_dir)
        target = os.path.join(data_dir, os.path.basename(os.path.normpath(args.outdir)))
        try:
            os.symlink(origin, target, target_is_directory=True)
            logging.info("Symlink to output directory created")
        except (FileExistsError, FileNotFoundError):
            logging.error(
                f"Error creating symlink to output in UserInterface/Data, "
                f"need to perform this manually for output to work! ln -s "
                f"{origin} {target}")

    logging.shutdown()
    # copy error log to output directory. tried move but the filehandle is
    # never released by logger no matter how hard i try
    shutil.copy("error.log", args.outdir + os.sep + "error.log")

if __name__ == '__main__':
    main()
