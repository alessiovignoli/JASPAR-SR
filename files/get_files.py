#!/usr/bin/env python

import argparse
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
from Bio.Alphabet import IUPAC
import coreapi
from functools import partial
import json
from multiprocessing import Pool
import os
from pathlib import Path
import pickle
# Download of Pfam/UniProt via RESTFUL API
from prody.database import pfam, uniprot
import re
import shutil
import subprocess
import sys
from tqdm import tqdm

# Defaults
out_dir = os.path.dirname(os.path.realpath(__file__))
root_dir = os.path.join(out_dir, os.pardir)

# Append JASPAR-profile-inference to path
sys.path.append(root_dir)

# Import globals
from __init__ import Jglobals
from infer_profile import hmmAlign, hmmScan, _makeSeqFile

#-------------#
# Functions   #
#-------------#

def parse_args():
    """
    This function parses arguments provided via the command line and returns
    an {argparse} object.
    """

    parser = argparse.ArgumentParser()

    parser.add_argument("-d", "--devel", action="store_true",
        help="development mode (uses hfaistos; default = False)")
    parser.add_argument("--dummy-dir", default="/tmp/", metavar="DIR",
        help="dummy directory (default = /tmp/)")
    parser.add_argument("-o", default=out_dir, metavar="DIR",
        help="output directory (default = ./)")
    parser.add_argument("--threads", default=1, type=int, metavar="INT",
        help="threads to use (default = 1)")

    return(parser.parse_args())

def main():

    # Parse arguments
    args = parse_args()

    # Get files
    get_files(args.devel, os.path.abspath(args.dummy_dir), os.path.abspath(args.o), args.threads)

def get_files(devel=False, dummy_dir="/tmp/", out_dir=out_dir, threads=1):

    # Globals
    global client
    global codec
    global cwd
    global pfam_file_ext
    global profiles_file_ext
    global uniprot_file_ext
    client = coreapi.Client()
    codec = coreapi.codecs.CoreJSONCodec()
    cwd = os.getcwd()
    pfam_file_ext = ".pfam.json"
    profiles_file_ext = ".profiles.json"
    uniprot_file_ext = ".uniprot.json"

    # Create output dir
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)

    # Get JASPAR URL
    global jaspar_url
    jaspar_url = "http://jaspar.genereg.net/"
    if devel:
        jaspar_url = "http://hfaistos.uio.no:8002/"

    # Download Pfam DBD hidden Markov models
    _download_Pfam_DBD_HMMs(out_dir)

    # For each taxon...
    for taxon in Jglobals.taxons:

        # Download JASPAR profiles
        _download_JASPAR_profiles(taxon, out_dir)

        # Get information for each profile
        _get_profile_info(taxon, out_dir)

        # Download TF sequences from UniProt
        _download_UniProt_sequences(taxon, out_dir)

        # Format BLAST+ database
        _format_BLAST_database(taxon, out_dir)

        # Get Pfam alignments
        _get_Pfam_alignments(taxon, out_dir)

    # Group TFs by DBD composition
    _group_by_DBD_composition(out_dir)

    # Group matrices by Tomtom similarity
    _group_by_Tomtom(out_dir, threads)

def _download_Pfam_DBD_HMMs(out_dir=out_dir):

    # Skip if Pfam DBD file already exists
    pfam_DBD_file = os.path.join(out_dir, "pfam-DBDs.json")
    if not os.path.exists(pfam_DBD_file):

        # Initialize
        cutoffs = {}
        pfam_DBDs = {}
        url = "http://cisbp.ccbr.utoronto.ca/data/2.00/DataFiles/Bulk_downloads/EntireDataset/"
        cisbp_file = "TF_Information_all_motifs.txt.zip"
        faulty_pfam_ids = {
            "DUF260": "LOB",
            "FLO_LFY": "SAM_LFY",
        }

        # Change dir
        os.chdir(out_dir)

        # Skip if Cis-BP file already exists
        if not os.path.exists(cisbp_file):

            # Download TF info
            os.system("curl --silent -O %s%s" % (url, cisbp_file))

        # Get DBD/cut-off pairs
        cmd = "unzip -p %s | cut -f 11,13 | sort | uniq | grep -v DBDs" % cisbp_file
        process = subprocess.run([cmd], shell=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        pairs = [l.split("\t") for l in process.stdout.decode("utf-8").split("\n") if len(l) > 0]

        # For each pair..
        for pfam_id, cutoff in pairs:

            # Skip if multiple Pfam IDs...
            if "," in pfam_id:
                continue

            # Skip if not Pfam ID
            if pfam_id == "UNKNOWN":
                continue

            # Fix faulty Pfam IDs
            if pfam_id in faulty_pfam_ids:
                pfam_id = faulty_pfam_ids[pfam_id]

            # Add Pfam ID cut-off
            cutoffs.setdefault(pfam_id, float(cutoff))

        # For each pair..
        for pfam_ids, cutoff in pairs:

            # Skip if only one Pfam ID...
            if "," not in pfam_ids:
                continue

            # For each Pfam ID...
            for pfam_id in pfam_ids.split(","):

                # Skip if not Pfam ID
                if pfam_id == "UNKNOWN":
                    continue

                # Fix faulty Pfam IDs
                if pfam_id in faulty_pfam_ids:
                    pfam_id = faulty_pfam_ids[pfam_id]

                # Add Pfam ID cut-off
                cutoffs.setdefault(pfam_id, float(cutoff))

        # Create Pfam dir
        pfam_dir = "pfam-DBDs"
        if not os.path.exists(pfam_dir):
            os.makedirs(pfam_dir)

        # Change dir
        os.chdir(pfam_dir)

        # For each Pfam ID...
        for pfam_id in sorted(cutoffs):

            # Fetch MSA from Pfam
            msa_file = pfam.fetchPfamMSA(pfam_id, alignment="seed")

            # For each line...
            for line in Jglobals.parse_file(msa_file):

                m = re.search("^#=GF\sID\s+(\S+)$", line)
                if m:
                    pfam_id_std = m.group(1)

                m = re.search("^#=GF\sAC\s+(PF\d{5}).\d+$", line)
                if m:
                    pfam_ac = m.group(1)
                    break

            # HMM build
            hmm_file = "%s.hmm" % pfam_id_std
            cmd = "hmmbuild %s %s" % (hmm_file, msa_file)
            process = subprocess.run([cmd], shell=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)

            # HMM press
            cmd = "hmmpress -f %s" % hmm_file
            process = subprocess.run([cmd], shell=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)

            # Add Pfam
            pfam_DBDs.setdefault(pfam_ac, [pfam_id_std, cutoffs[pfam_id]])

            # Remove MSA file
            os.remove(msa_file)

        # Skip if HMM database of all DBDs already exists
        hmm_db = "all_DBDs.hmm"
        if not os.path.exists(hmm_db):

            # For each HMM file...
            for hmm_file in os.listdir("."):

                # Skip if not HMM file
                if not hmm_file.endswith(".hmm"): continue

                # Add HMM to database
                for line in Jglobals.parse_file(hmm_file):
                    Jglobals.write(hmm_db, line)

            # HMM press
            cmd = "hmmpress -f %s" % hmm_db
            process = subprocess.run([cmd], shell=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Write
        Jglobals.write(
            pfam_DBD_file,
            json.dumps(pfam_DBDs, sort_keys=True, indent=4, separators=(",", ": "))
        )

        # Change dir
        os.chdir(out_dir)

        # Remove Cis-BP file
        if os.path.exists(cisbp_file):
            os.remove(cisbp_file)

    # Change dir
    os.chdir(cwd)

def _download_JASPAR_profiles(taxon, out_dir=out_dir):
        
    # Skip if taxon directory already exists
    taxon_dir = os.path.join(out_dir, taxon)
    if not os.path.exists(taxon_dir):

        # Create taxon directory
        os.makedirs(taxon_dir)

        # Move to taxon directory
        os.chdir(taxon_dir)

        for fmt in ["jaspar", "meme"]:

            # Initialize
            jaspar_file = "JASPAR2020_CORE_%s_redundant_pfms_%s.zip" % (taxon, fmt)
            if "hfaistos.uio.no:8002" in jaspar_url:
                jaspar_file = "JASPAR2020_CORE_%s_redundant_pfms_%s.zip" % (taxon, fmt)

            # Get JASPAR profiles
            os.system("curl --silent -O %s" % os.path.join(jaspar_url, "download",
                "CORE", jaspar_file))

            # Unzip
            os.system("unzip -qq %s" % jaspar_file)

            # Remove zip files
            os.remove("%s" % jaspar_file)

        # Change dir
        os.chdir(cwd)

def _get_profile_info(taxon, out_dir=out_dir):

    # Skip if taxon profiles JSON file already exists
    profiles_json_file = os.path.join(out_dir, taxon + profiles_file_ext)
    if not os.path.exists(profiles_json_file):

        # Initialize
        profiles = {}
        url = os.path.join(jaspar_url, "api", "v1", "taxon", taxon)
        response = client.get(url)
        json_obj = json.loads(codec.encode(response))

        # While there are more pages...
        while json_obj["next"] is not None:

            # For each profile...
            for profile in json_obj["results"]:

                # Add profiles from the CORE collection...
                if profile["collection"] == "CORE":
                    profiles.setdefault(profile["matrix_id"], profile["name"])

            # Go to next page
            response = client.get(json_obj["next"])
            json_obj = json.loads(codec.encode(response))

        # Do last page
        for profile in json_obj["results"]:

            # Add profiles from the CORE collection...
                if profile["collection"] == "CORE":
                    profiles.setdefault(profile["matrix_id"], profile["name"])

        # Write
        Jglobals.write(
            profiles_json_file,
            json.dumps(profiles, sort_keys=True, indent=4, separators=(",", ": "))
        )

def _download_UniProt_sequences(taxon, out_dir=out_dir):

    # Initialize
    faulty_profiles = {
        "MA0024.1": ["Q01094"],
        "MA0046.1": ["P20823"],
        "MA0052.1": ["Q02078"],
        "MA0058.1": ["P61244"],
        "MA0098.1": ["P14921"],
        "MA0110.1": ["P46667"],
        "MA0138.1": ["Q13127"],
        "MA0328.1": ["P0CY08"],
        "MA0529.1": ["Q94513"],
        "MA0529.2": ["Q94513"]
    }
    faulty_sequences = {
        "B9GPL8": [
            "MEEVGAQVAAPIFIHEALSSRYCDMTSMAKKHDLSYQSPNSQLQQHQFLQASREKNWNSK",
            "AWDWDSVDDDGLGLNLGGSLTSVEEPVSRPNKRVRSGSPGNGSYPMCQVDNCKEDLSKAK",
            "DYHRRHKVCQVHSKATKALVGKQMQRFCQQCSRFHPLTEFDEGKRSCRRRLAGHNRRRRK",
            "TQPEDVTSRLLLPGNPDMNNNGNLDIVNLLTALARSQGKTYLPMIDFYVPPFVLTNCPTV",
            "PDKDQLIQILNKINSLPLPMDLAAKLSNIASLNVKNPNQPYLGHQNRLNGTASSPSTNDL",
            "LAVLSTTLAASAPDALAILSQRSSQSSDNDKSKLPGPNQVTVPHLQKRSNVEFPAVGVER",
            "ISRCYESPAEDSDYQIQESRPNLPLQLFSSSPENESRQKPASSGKYFSSDSSNPIEERSP",
            "SSSPPVVQKLFPLQSTAETMKSEKMSVSREVNANVEGDRSHGCVLPLELFRGPNREPDHS",
            "SFQSFPYRGGYTSSSGSDHSPSSQNSDPQDRTGRIIFKLFDKDPSHFPGTLRTKIYNWLS",
            "NSPSEMESYIRPGCVVLSVYLSMPSASWEQLERNLLQLVDSLVQDSDSDLWRSGRFLLNT",
            "GRQLASHKDGKVRLCKSWRTWSSPELILVSPVAVIGGQETSLQLKGRNLTGPGTKIHCTY",
            "MGGYTSKEVTDSSSPGSMYDEINVGGFKIHGPSPSILGRCFIEVENGFKGNSFPVIIADA",
            "SICKELRLLESEFDENAVVSNIVSEEQTRDLGRPRSREEVMHFLNELGWLFQRKSMPSMH",
            "EAPDYSLNRFKFLLIFSVERDYCVLVKTILDMLVERNTCRDELSKEHLEMLYEIQLLNRS",
            "VKRRCRKMADLLIHYSIIGGDNSSRTYIFPPNVGGPGGITPLHLAACASGSDGLVDALTN",
            "DPHEIGLSCWNSVLDANGLSPYAYAVMTKNHSYNLLVARKLADKRNGQISVAIGNEIEQA",
            "ALEQEHVTISQFQRERKSCAKCASVAAKMHGRFLGSQGLLQRPYVHSMLAIAAVCVCVCL",
            "FFRGAPDIGLVAPFKWENLNYGTI"
        ]
    }

    # Change dir
    os.chdir(out_dir)

    # Skip if pickle file already exists
    pickle_file = ".%s.uniaccs.pickle" % taxon
    if not os.path.exists(pickle_file):

        # Initialize
        uniaccs = {}

        # Load JSON file
        profiles_json_file = taxon + profiles_file_ext
        with open(profiles_json_file) as f:
            profiles = json.load(f)

        # For each profile...
        for profile in sorted(profiles):

            # Get profile detailed info
            url = os.path.join(jaspar_url, "api", "v1", "matrix", profile)
            response = client.get(url)
            json_obj = json.loads(codec.encode(response))

            # Fix faulty profiles
            if json_obj["matrix_id"] in faulty_profiles:
                json_obj["uniprot_ids"] = faulty_profiles[json_obj["matrix_id"]]

            # For each UniProt Accession...
            for uniacc in json_obj["uniprot_ids"]:

                # Initialize
                uniacc = uniacc.strip(" ")
                uniaccs.setdefault(uniacc, [[], None])

                # Add uniacc
                if profile not in uniaccs[uniacc][0]:
                    uniaccs[uniacc][0].append(profile)

        # Write pickle file
        with open(pickle_file, "wb") as f:
            pickle.dump(uniaccs, f)

    # Skip if taxon uniprot JSON file already exists
    uniprot_json_file = taxon + uniprot_file_ext
    if not os.path.exists(uniprot_json_file):

        # Load pickle file
        with open(pickle_file, "rb") as f:
            uniaccs = pickle.load(f)

        # For each UniProt Accession...
        for uniacc in uniaccs:

            # Fix faulty sequences
            if uniacc in faulty_sequences:
                uniaccs[uniacc][1] = "".join(faulty_sequences[uniacc]) 
                continue

            # Get UniProt sequence
            u = uniprot.queryUniprot(uniacc)
            uniaccs[uniacc][1] = "".join(u["sequence   0"].split("\n"))

        # Write
        Jglobals.write(
            uniprot_json_file,
            json.dumps(uniaccs, sort_keys=True, indent=4, separators=(",", ": "))
        )

    # Change dir
    os.chdir(cwd)

def _format_BLAST_database(taxon, out_dir=out_dir):

    # Skip if taxon FASTA file already exists
    fasta_file = os.path.join(out_dir, "%s.fa" % taxon)
    if not os.path.exists(fasta_file):

        # Load JSON file
        uniprot_json_file = taxon + uniprot_file_ext
        with open(uniprot_json_file) as f:
            uniaccs = json.load(f)

        # For each UniProt Accession...
        for uniacc in sorted(uniaccs):
            Jglobals.write(fasta_file, ">%s\n%s" % (uniacc, uniaccs[uniacc][1]))

        # Make BLAST+ database
        cmd = "makeblastdb -in %s -dbtype prot" % fasta_file
        process = subprocess.run([cmd], shell=True, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)

def _get_Pfam_alignments(taxon, out_dir=out_dir):

    # Skip if Pfam JSON file already exists
    pfam_json_file = os.path.join(out_dir, taxon + pfam_file_ext)
    if not os.path.exists(pfam_json_file):

        # Change dir
        os.chdir(out_dir)

        # Initialize
        pfams = {}
        seq_file = ".seq.fasta"
        hmm_db = os.path.join("pfam-DBDs", "all_DBDs.hmm")
        uniprot_json_file = taxon + uniprot_file_ext

        # Load JSON file
        with open(uniprot_json_file) as f:
            uniaccs = json.load(f)

        # For each uniacc...
        for uniacc in uniaccs:

            # Initialize
            pfams.setdefault(uniacc, [])

            # Make seq file
            seq = Seq(uniaccs[uniacc][1], IUPAC.protein)
            seq_record = SeqRecord(seq, id=uniacc, name=uniacc, description=uniacc)
            _makeSeqFile(seq_record, seq_file)

            # For each DBD...
            for pfam_ac, start, end, evalue in hmmScan(seq_file, hmm_db, non_overlapping_domains=True):

                # Initialize
                hmm_file = os.path.join("pfam-DBDs", "%s.hmm" % pfam_ac)

                # Make seq file
                sub_seq = seq[start:end]
                seq_record = SeqRecord(sub_seq, id=uniacc, name=uniacc, description=uniacc)
                _makeSeqFile(seq_record, seq_file)

                # Add DBDs
                alignment = hmmAlign(seq_file, hmm_file)
                pfams[uniacc].append((pfam_ac, alignment, start+1, end, evalue))

        # Write
        Jglobals.write(
            pfam_json_file,
            json.dumps(pfams, sort_keys=True, indent=4, separators=(",", ": "))
        )

        # Remove seq file
        if os.path.exists(seq_file):
            os.remove(seq_file)

        # Change dir
        os.chdir(cwd)

def _group_by_DBD_composition(out_dir=out_dir):

    # Skip if groups JSON file already exists
    gzip_file = os.path.join(out_dir, "groups.DBDs.json.gz")
    if not os.path.exists(gzip_file):

        # Initialize
        groups = {}

        # Move to output directory
        os.chdir(out_dir)

        # For each taxon...
        for taxon in Jglobals.taxons:

            # Load JSON files
            pfam_json_file = "%s.pfam.json" % taxon
            with open(pfam_json_file) as f:
                pfams = json.load(f)
            uniprot_json_file = "%s.uniprot.json" % taxon
            with open(uniprot_json_file) as f:
                uniaccs = json.load(f)

            # For each uniacc...
            for uniacc, values in pfams.items():

                # Unwind
                domains = "+".join([v[0] for v in values])
                alignments = [v[1] for v in values]

                # Skip if no DBD
                if domains == "":
                    continue

                # Add member
                groups.setdefault(domains, [])
                groups[domains].append([uniaccs[uniacc][0], alignments, uniacc])

        # Write
        Jglobals.write(
            gzip_file[:-3],
            json.dumps(groups, sort_keys=True, indent=4, separators=(",", ": "))
        )
        fi = Jglobals._get_file_handle(gzip_file[:-3], "rb")
        fo = Jglobals._get_file_handle(gzip_file, "wb")
        shutil.copyfileobj(fi, fo)
        fi.close()
        fo.close()
        os.remove(gzip_file[:-3])

        # Change dir
        os.chdir(cwd)

def _group_by_Tomtom(out_dir=out_dir, threads=1):

    # Skip if groups JSON file already exists
    gzip_file = os.path.join(out_dir, "groups.tomtom.json.gz")
    if not os.path.exists(gzip_file):

        # Initialize
        tomtom = {}

        # Get all JASPAR profiles
        jaspar_profiles = _get_profiles_from_latest_version(Path(out_dir).glob("*/*.meme"))
        # jaspar_profiles = [str(f) for f in Path(out_dir).glob("*/*.meme")]

        # Skip if JASPAR MEME database already exists
        database = os.path.join(out_dir, "jaspar.meme")
        if not os.path.exists(database):

            # For each JASPAR profile...
            for jaspar_profile in jaspar_profiles:

                # Cat to database
                os.system("cat %s >> %s" % (jaspar_profile, database))

        # Parallelize
        pool = Pool(threads)
        parallelized = partial(Tomtom, database=database, out_dir=out_dir)
        for _ in tqdm(
            pool.imap(parallelized, jaspar_profiles), desc="Tomtom", total=len(jaspar_profiles)
        ):
            pass
        pool.close()
        pool.join()

        # Move to output directory
        os.chdir(out_dir)

        # For each JASPAR profile...
        for jaspar_profile in jaspar_profiles:

            # Initialize
            m = re.search("(MA\d{4}.\d).meme$", jaspar_profile)
            tomtom_dir = ".%s" % m.group(1)

            # Get hits
            tomtom.setdefault(m.group(1), _get_Tomtom_hits(tomtom_dir))

            # Remove Tomtom directory
            shutil.rmtree(tomtom_dir)

        # Write
        Jglobals.write(
            gzip_file[:-3],
            json.dumps(tomtom, sort_keys=True, indent=4, separators=(",", ": "))
        )
        fi = Jglobals._get_file_handle(gzip_file[:-3], "rb")
        fo = Jglobals._get_file_handle(gzip_file, "wb")
        shutil.copyfileobj(fi, fo)
        fi.close()
        fo.close()
        os.remove(gzip_file[:-3])

        # For each taxon...
        for taxon in Jglobals.taxons:

            # Remove taxon directory
            if os.path.isdir(taxon):
                shutil.rmtree(taxon)

        # Change dir
        os.chdir(cwd)

def Tomtom(meme_file, database, out_dir=out_dir):
    """
    From http://meme-suite.org/doc/tomtom.html;
    In order to compute the scores, Tomtom needs to know the frequencies of
    the letters of the sequence alphabet in the database being searched (the
    "background" letter frequencies). By default, the background letter fre-
    quencies included in the query motif file are used. The scores of columns
    that overlap for a given offset are summed. This summed score is then con-
    verted to a p-value. The reported p-value is the minimal p-value over all
    possible offsets. To compensate for multiple testing, each reported p-value
    is converted to an E-value by multiplying it by twice the number of target
    motifs. As a second type of multiple-testing correction, q-values for each
    match arecomputed from the set of p-values and reported.
    """

    # Skip if output directory already exists
    m = re.search("(MA\d{4}.\d|M\d{4}_1.02).meme$", meme_file)
    output_dir = os.path.join(out_dir, ".%s" % m.group(1))
    if not os.path.isdir(output_dir):

        # Run Tomtom
        cmd = "tomtom -o %s -thresh 10000 -evalue %s %s" % (output_dir, meme_file, database)
        process = subprocess.run([cmd], shell=True, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)

def _get_profiles_from_latest_version(jaspar_profiles):

    # Initialize
    done = set()
    latest_version_profiles = []

    # For each profile...
    for jaspar_profile in sorted(jaspar_profiles, reverse=True):

        # Initialize
        m = re.search("(MA\d{4}).\d.\S+$", str(jaspar_profile))
        matrix_id = m.group(1)

        # Skip if done
        if matrix_id in done:
            continue

        # i.e. a profile from the latest version
        latest_version_profiles.append(str(jaspar_profile))

        # Done
        done.add(matrix_id)

    return(latest_version_profiles)

def _get_Tomtom_hits(tomtom_dir):

    # Intialize
    hits = []

    # For each line...
    for line in Jglobals.parse_tsv_file(os.path.join(tomtom_dir, "tomtom.tsv")):

        # Skip comments
        if line[0].startswith("#"):
            continue

        # Skip header
        if line[0] == "Query_ID":
            continue

        # Skip self
        if line[0][:6] == line[1][:6]:
            continue

        # Add to hits
        hits.append([line[1], float(line[4])])

    return(hits)

#-------------#
# Main        #
#-------------#

if __name__ == "__main__":

    main()