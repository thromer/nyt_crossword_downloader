"""
there are two rate limits per API: 4,000 requests per day and 10 requests per
minute. You should sleep 6 seconds between calls to avoid hitting the per
minute rate limit.
"""


import requests
import os
import json
import re
import sys

from time import time, sleep
from datetime import datetime, timedelta
from dateutil.parser import parse as parse_dt_str
from argparse import ArgumentParser


def normalize_date_str(date_str):
    (y, m, d) = date_str.split("-")
    return "{:04d}-{:02d}-{:02d}".format(int(y), int(m), int(d))


class MissingPuzzleData(Exception):
    pass


class CLIArgs:
    def __init__(self):
        self.parser = self.build_parser()

    def parse(self):
        return self.parser.parse_args()

    def build_parser(self):
        today = Puzzle.format_date(datetime.now())
        parser = ArgumentParser(description="Download NYT crossword puzzles.")
        parser.add_argument(
            "destination",
            default=".",
            help="Folder where crossword data will be written. Default is current directory.",
        )
        parser.add_argument(
            "--cookie_string", "-c", help="NYT-S=<cookie value>"
        )
        parser.add_argument(
            "--start",
            "-s",
            default=today,
            help="Download puzzles starting on date.",
        )
        parser.add_argument(
            "--end",
            "-e",
            default=today,
            help="Download puzzles ending on date.",
        )
        parser.add_argument(
            "--interval_seconds",
            "-i",
            type=float,
            default=30,
            help="Delay between requests"
        )
        parser.add_argument(
            "--puzzle-id", "-p", type=int, help="Download a particular puzzle ID."
        )
        parser.add_argument(
            "--date-folders",
            action="store_true",
            help="Place downloaded puzzles into folders organized by year and month. Default is completely flat folder structure.",
        )
        return parser


class Puzzle:
    URL_RECENT_PUZZLES = "https://nytimes.com/svc/crosswords/v3/puzzles.json?publish_type=daily&sort_order=asc&sort_by=print_date&date_start={date_start}&date_end={date_end}"
    URL_PUZZLE_BY_ID = (
        "https://www.nytimes.com/svc/crosswords/v6/puzzle/{puzzle_id}.json"
    )

    def __init__(self, cookies):
        self.cookies = cookies

    def get_results_from_json(self, data):
        try:
            return data["results"][0]
        except Exception:
            raise MissingPuzzleData("No data could be found for this puzzle!")

    def get_puzzle_ids_by_dates(self, start_dt, end_dt):
        sd = self.format_date(start_dt)
        ed = self.format_date(end_dt)
        url = self.URL_RECENT_PUZZLES.format(date_start=sd, date_end=ed)
        # print(url)
        resp = requests.get(url)
        result = {}
        for r in resp.json()["results"]:
            result[parse_dt_str(normalize_date_str(r["print_date"]))] = r["puzzle_id"]
        return result

    def get_puzzle_date_str(self, data, day_only=False):
        if day_only:
            return self.zero_pad_two(self.get_puzzle_date(data).day)
        return self.get_puzzle_date(data, return_date_str=True)

    def get_puzzle_date(self, data, return_date_str=False):
        try:
            date_str = data["publicationDate"]
        except KeyError:
            raise MissingPuzzleData("No data could be found for this puzzle!")
        else:
            # Normalize since sometimes publicationDate is wonky (e.g. 1993-12-4)
            date_str = normalize_date_str(date_str)
            if return_date_str:
                return date_str
            return parse_dt_str(date_str)

    @classmethod
    def format_date(self, dt):
        return dt.strftime("%Y-%m-%d")

    @classmethod
    def zero_pad_two(self, n):
        return "{:02d}".format(n)

    def get_puzzle_data_by_date(self, dt):
        puzzle_id = self.get_puzzle_ids_by_dates(dt, dt)[dt]
        _, _, data = self.get_puzzle_data_by_id(puzzle_id)
        return puzzle_id, dt, data

    def get_puzzle_data_by_id(self, puzzle_id):
        url = self.URL_PUZZLE_BY_ID.format(puzzle_id=puzzle_id)
        # print(url)
        resp = requests.get(url, cookies=self.cookies.cookies)
        # print(resp.json())
        data = resp.json()  # self.get_results_from_json(resp.json())
        # check that the board is there ...
        _ = data["body"][0]["board"]
        return puzzle_id, self.get_puzzle_date(data), data


class Cookies:
    def __init__(self, cookie_string):
        self.cookie_string = cookie_string

    @property
    def cookies(self):
        return self.parse(self.cookie_string)

    def parse(self, cookie_string):
        """Load and parse cookie=value file into a dict."""
        cookies = {}
        if cookie_string is None:
            return cookies
        k, v = cookie_string.split('=', 1)
        cookies[k] = v
        return cookies


class FileSystem:
    def __init__(self, puzzle, destination_folder, date_folders=False):
        self.puzzle = puzzle
        self.destination = destination_folder
        self.date_folders = date_folders

    def get_destination_root(self, dt=None):
        if self.date_folders:
            return os.path.join(
                self.destination, str(dt.year), self.puzzle.zero_pad_two(dt.month)
            )
        else:
            return self.destination

    def make_destination_folder_if_not_exists(self, dt):
        try:
            root = self.get_destination_root(dt)
            os.makedirs(root, exist_ok=True)
        except Exception as error:
            raise Exception(f"Cannot create destination directory: {error}")
        else:
            return root

    def write_to_disk(self, puzzle_id, dt, data):
        root = self.make_destination_folder_if_not_exists(dt)
        filename = self.puzzle.get_puzzle_date_str(data, day_only=self.date_folders)
        path = os.path.join(root, f"{filename}.json")
        with open(path, "w") as fd:
            json.dump(data, fd, indent=2)
        return path


class RangeDownloader:
    def __init__(
        self, destination=".", cookie_string=None, date_folders=False, secs_btwn_queries=0
    ):
        self.destination = destination
        self.cookie_string = cookie_string
        self.date_folders = date_folders
        self.secs_btwn_queries = secs_btwn_queries
        self.puzzle = Puzzle(Cookies(self.cookie_string))
        self.fs = FileSystem(self.puzzle, self.destination, date_folders)

    def make_date_range(self, start_date, stop_date):
        if stop_date < start_date:
            raise ValueError("Stop date must come after start date.")
        n_days = (stop_date - start_date).days + 1  # add one so we end on stop date
        return [start_date + timedelta(days=x) for x in range(n_days)]

    def download_date_range(
        self, start_date, stop_date,
    ):
        PAGE_SIZE = 100
        run_st = time()
        time_waiting = 0

        ids = {}
        # First let's get *all* the ids
        current_date = start_date
        while current_date <= stop_date:
            query_st = time()
            ids.update(self.puzzle.get_puzzle_ids_by_dates(current_date, min(stop_date, current_date + timedelta(days=PAGE_SIZE-1))))
            current_date = current_date + timedelta(days=PAGE_SIZE)
            query_elapsed = time() - query_st
            time_remaining = self.secs_btwn_queries - query_elapsed
            if time_remaining > 0:
                time_waiting += time_remaining
                # print(f"[ids] sleep {time_remaining}", file=sys.stderr)
                sleep(time_remaining)

        date_list = self.make_date_range(start_date, stop_date)  # WTF is this?
        for date in date_list:
            query_st = time()
            try:
                (
                    puzzle_id,
                    puzzle_date,
                    puzzle_data,
                ) = self.puzzle.get_puzzle_data_by_id(ids[date])
            except Exception:
                pass
            else:
                path = self.fs.write_to_disk(puzzle_id, puzzle_date, puzzle_data)
                puzzle_date_str = self.puzzle.format_date(puzzle_date)
                print(
                    f"Downloaded {puzzle_date_str} puzzle (ID: {puzzle_id}) to: {path}"
                )
            # Figure out time elapsed since query started. If we are still under
            # `secs_btwn_queries`, sleep for the remaining time.
            query_elapsed = time() - query_st
            time_remaining = self.secs_btwn_queries - query_elapsed
            if time_remaining > 0:
                time_waiting += time_remaining
                # print(f"sleep {time_remaining}", file=sys.stderr)
                sleep(time_remaining)
        print(
            "Finished downloading date range in {:.02f} seconds.".format(
                time() - run_st
            )
        )
        print(
            "{:.02f} seconds of that was spent waiting to avoid rate limits.".format(
                time_waiting
            )
        )

    def download_id_range(self, start_id, stop_id, cookie_string=None):
        raise NotImplementedError


def main():
    args = CLIArgs().parse()
    r = RangeDownloader(args.destination, args.cookie_string, args.date_folders, args.interval_seconds)
    r.download_date_range(
        parse_dt_str(args.start),
        parse_dt_str(args.end)
    )
    sys.exit(0)
    cookies = Cookies(args.cookie_string)
    puzzle = Puzzle(cookies)
    file_system = FileSystem(puzzle, args.destination, args.date_folders)

    try:
        if args.puzzle_id is None:
            puzzle_id, puzzle_date, puzzle_data = puzzle.get_puzzle_data_by_date(
                parse_dt_str(args.end)
            )
        else:
            puzzle_id, puzzle_date, puzzle_data = puzzle.get_puzzle_data_by_id(
                args.puzzle_id
            )
    except Exception as error:
        print(str(error))
        exit(1)
    else:
        path = file_system.write_to_disk(puzzle_id, puzzle_date, puzzle_data)
        puzzle_date_str = puzzle.format_date(puzzle_date)
        print(f"Downloaded {puzzle_date_str} puzzle (ID: {puzzle_id}) to: {path}")
