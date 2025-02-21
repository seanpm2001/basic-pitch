#!/usr/bin/env python
# encoding: utf-8
#
# Copyright 2024 Spotify AB
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
import numpy as np
import os
import pathlib
import wave

from mido import MidiFile, MidiTrack, Message
from typing import List

import apache_beam as beam
from apache_beam.testing.test_pipeline import TestPipeline

from basic_pitch.data.datasets.maestro import (
    MaestroToTfExample,
    MaestroInvalidTracks,
    create_input_data,
)
from basic_pitch.data.pipeline import WriteBatchToTfRecord

RESOURCES_PATH = pathlib.Path(__file__).parent.parent / "resources"
MAESTRO_TEST_DATA_PATH = RESOURCES_PATH / "data" / "maestro"

TRAIN_TRACK_ID = "2004/MIDI-Unprocessed_SMF_05_R1_2004_01_ORIG_MID--AUDIO_05_R1_2004_03_Track03_wav"
VALID_TRACK_ID = "2004/MIDI-Unprocessed_SMF_02_R1_2004_01-05_ORIG_MID--AUDIO_02_R1_2004_06_Track06_wav"
TEST_TRACK_ID = "2004/MIDI-Unprocessed_SMF_02_R1_2004_01-05_ORIG_MID--AUDIO_02_R1_2004_08_Track08_wav"
GT_15M_TRACK_ID = "2004/MIDI-Unprocessed_SMF_02_R1_2004_01-05_ORIG_MID--AUDIO_02_R1_2004_05_Track05_wav"


def create_mock_wav(output_fpath: str, duration_min: int) -> None:
    duration_seconds = duration_min * 60
    sample_rate = 44100
    n_channels = 2  # Stereo
    sampwidth = 2  # 2 bytes per sample (16-bit audio)

    # Generate a silent audio data array
    num_samples = duration_seconds * sample_rate
    audio_data = np.zeros((num_samples, n_channels), dtype=np.int16)

    # Create the WAV file
    with wave.open(str(output_fpath), "w") as wav_file:
        wav_file.setnchannels(n_channels)
        wav_file.setsampwidth(sampwidth)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(audio_data.tobytes())

    logging.info(f"Mock {duration_min}-minute WAV file '{output_fpath}' created successfully.")


def create_mock_midi(output_fpath: str) -> None:
    # Create a new MIDI file with one track
    mid = MidiFile()
    track = MidiTrack()
    mid.tracks.append(track)

    # Define a sequence of notes (time, type, note, velocity)
    notes = [
        (0, "note_on", 60, 64),  # C4
        (500, "note_off", 60, 64),
        (0, "note_on", 62, 64),  # D4
        (500, "note_off", 62, 64),
    ]

    # Add the notes to the track
    for time, type, note, velocity in notes:
        track.append(Message(type, note=note, velocity=velocity, time=time))

    # Save the MIDI file
    mid.save(output_fpath)

    logging.info(f"Mock MIDI file '{output_fpath}' created successfully.")


def test_maestro_to_tf_example(tmp_path: pathlib.Path) -> None:
    mock_maestro_home = tmp_path / "maestro"
    mock_maestro_ext = mock_maestro_home / "2004"
    mock_maestro_ext.mkdir(parents=True, exist_ok=True)

    create_mock_wav(str(mock_maestro_ext / (TRAIN_TRACK_ID.split("/")[1] + ".wav")), 3)
    create_mock_midi(str(mock_maestro_ext / (TRAIN_TRACK_ID.split("/")[1] + ".midi")))

    output_dir = tmp_path / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    input_data: List[str] = [TRAIN_TRACK_ID]
    with TestPipeline() as p:
        (
            p
            | "Create PCollection of track IDs" >> beam.Create([input_data])
            | "Create tf.Example" >> beam.ParDo(MaestroToTfExample(str(mock_maestro_home), download=False))
            | "Write to tfrecord" >> beam.ParDo(WriteBatchToTfRecord(str(output_dir)))
        )

    listdir = os.listdir(str(output_dir))
    assert len(listdir) == 1
    assert os.path.splitext(listdir[0])[-1] == ".tfrecord"
    with open(os.path.join(str(output_dir), os.listdir(str(output_dir))[0]), "rb") as fp:
        data = fp.read()
        assert len(data) != 0


def test_maestro_invalid_tracks(tmp_path: pathlib.Path) -> None:
    mock_maestro_home = tmp_path / "maestro"
    mock_maestro_ext = mock_maestro_home / "2004"
    mock_maestro_ext.mkdir(parents=True, exist_ok=True)

    input_data = [(TRAIN_TRACK_ID, "train"), (VALID_TRACK_ID, "validation"), (TEST_TRACK_ID, "test")]

    for track_id, _ in input_data:
        create_mock_wav(str(mock_maestro_ext / (track_id.split("/")[1] + ".wav")), 3)

    split_labels = set([e[1] for e in input_data])
    with TestPipeline() as p:
        splits = (
            p
            | "Create PCollection" >> beam.Create(input_data)
            | "Tag it" >> beam.ParDo(MaestroInvalidTracks(str(mock_maestro_home))).with_outputs(*split_labels)
        )

        for split in split_labels:
            (
                getattr(splits, split)
                | f"Write {split} to text"
                >> beam.io.WriteToText(str(tmp_path / f"output_{split}.txt"), shard_name_template="")
            )

    for track_id, split in input_data:
        with open(tmp_path / f"output_{split}.txt", "r") as fp:
            assert fp.read().strip() == track_id


def test_maestro_invalid_tracks_over_15_min(tmp_path: pathlib.Path) -> None:
    """
    The track id used here is a real track id in maestro, and it is part of the train split, but we mock the data so as
    not to store a large file in git, hence the variable name.
    """

    mock_maestro_home = tmp_path / "maestro"
    mock_maestro_ext = mock_maestro_home / "2004"
    mock_maestro_ext.mkdir(parents=True, exist_ok=True)

    mock_fpath = mock_maestro_ext / (GT_15M_TRACK_ID.split("/")[1] + ".wav")
    create_mock_wav(str(mock_fpath), 16)

    input_data = [(GT_15M_TRACK_ID, "train")]
    split_labels = set([e[1] for e in input_data])
    with TestPipeline() as p:
        splits = (
            p
            | "Create PCollection" >> beam.Create(input_data)
            | "Tag it" >> beam.ParDo(MaestroInvalidTracks(str(mock_maestro_home))).with_outputs(*split_labels)
        )

        for split in split_labels:
            (
                getattr(splits, split)
                | f"Write {split} to text"
                >> beam.io.WriteToText(str(tmp_path / f"output_{split}.txt"), shard_name_template="")
            )

    for _, split in input_data:
        with open(tmp_path / f"output_{split}.txt", "r") as fp:
            assert fp.read().strip() == ""


def test_maestro_create_input_data() -> None:
    """
    A commuted metadata file is included in the repo for testing. mirdata references the metadata file to
    populate the tracklist with metadata. Since the file is commuted to only the filenames referenced here,
    we only consider these when testing the metadata.
    """
    data = create_input_data(str(MAESTRO_TEST_DATA_PATH))
    assert len(data)

    test_fnames = {TRAIN_TRACK_ID, VALID_TRACK_ID, TEST_TRACK_ID, GT_15M_TRACK_ID}
    splits = {d[1] for d in data if d[0].split(".")[0] in test_fnames}
    assert splits == set(["train", "validation", "test"])
