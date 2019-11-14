#!/usr/bin/env python
"""
predictPAM: A python module to predict custom PAM sites in any small genome

"""
# Author: Ravin Poudel

import sys
import os
import logging
import shutil
import random
import itertools
import subprocess
import numpy
import tempfile
import argparse
import string
import pickle
import nmslib
import numpy as np
import pandas as pd
from Bio import SeqIO
from Bio.Alphabet import IUPAC
from itertools import permutations
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
from pybedtools import BedTool
from collections import Counter

def myparser():
    parser = argparse.ArgumentParser(description='predictPAM: A python module to predict custom PAM sites in any small genome')
    parser.add_argument('--gbkfile', '-i', type=str, required=True,help='A genbank .gbk file')
    parser.add_argument('--pamseq', '-p', type=str, required=True, help='A short PAM motif to search for, may be use IUPAC ambiguous alphabet')
    parser.add_argument('--targetlength', '-l', type=int, default=22, help='Length of the target sequence')
    parser.add_argument('--strand', '-s', choices=['forward','reverse'], default='forward', help='Strand of DNA') # use choices array,  use 'plus' and 'minus"
    parser.add_argument('--lcp', type=int, default=12, help='Length of convered sequence close to PAM')
    parser.add_argument('--eds', type=int, choices=range(6),default=2, help='Unexcepted Levenshtein edit distance on the distal portion of target sequence from PAM')
    parser.add_argument('--outfile', '-o', type=str, required=True, help='The table of pam sites and data')
    parser.add_argument('--tempdir', help='The temp file directory', default=None)
    parser.add_argument('--keeptemp' ,help="Should intermediate files be kept?", action='store_true')
    parser.add_argument('--log' ,help="Log file", default="predictPAM.log")
    parser.add_argument('--threads' ,help="Number of processor threads to use.", type=int, default=1)
    return parser

# GLOBAL parsing variables

def _logger_setup(logfile):
    """Set up logging to a logfile and the terminal standard out.

    Args:
        fastq (str): The path to a fastq or fastq.gz file
        fastq2 (str): The path to a fastq or fastq.gz file for the reverse sequences

    """
    try:
        logging.basicConfig(level=logging.DEBUG,
                            format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
                            datefmt='%m-%d %H:%M',
                            filename=logfile,
                            filemode='w')
        # define a Handler which writes INFO messages or higher to the sys.stderr
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        # set a format which is simpler for console use
        formatter = logging.Formatter('%(asctime)s: %(levelname)-8s %(message)s')
        # tell the handler to use this format
        console.setFormatter(formatter)
        # add the handler to the root logger
        logging.getLogger('').addHandler(console)
    except Exception as e:
        print("An error occurred setting up logging")
        raise e

def get_fastas(genbank, tempdir):
    """Returns Fasta and complement of Fasta for a given Genbank file

    Args:
        genbank (str): Genbank file to process

    Returns:
        (str): out.fasta path, Fasta file in forward orientation (5'-3')
        (str): out_complement.fasta path, Complement of Fasta file

    """
    # try:
    #     gbk_indx = SeqIO.index(genbank,"genbank")
    #
    #
    #     sequences = []
    #     sequences_complement=[]
    #     for record in SeqIO.parse(genbank, "genbank"):
    #         sequences.append(record)
    #         sequences_complement.append(SeqRecord(record.seq.complement(), record.id+"_complement",
    #         description=record.description+"_complement", name=record.name+"_complement"))
    #     SeqIO.write(sequences, os.path.join(tempdir,"out.fasta"), "fasta")
    #     SeqIO.write(sequences_complement, os.path.join(tempdir,"out_complement.fasta"), "fasta")
    # except Exception as e:
    #     print("An error occurred in input genbank file")
    #     raise e
    try:
        sequences = []
        sequences_complement=[]
        for record in SeqIO.parse(genbank, "genbank"):
            sequences.append(record)
            sequences_complement.append(SeqRecord(record.seq.complement(), record.id+"_complement",
            description=record.description+"_complement", name=record.name+"_complement"))
        SeqIO.write(sequences, os.path.join(tempdir,"out.fasta"), "fasta")
        SeqIO.write(sequences_complement, os.path.join(tempdir,"out_complement.fasta"), "fasta")
    except Exception as e:
        print("An error occurred in input genbank file")
        raise e
    


def map_pam(tempdir, pamseq, threads, strand):
    """Runs seqkit locate to find the PAM site in the genome (Fasta)

    Args:
        threads (int): the number of processor threads to use

    Returns:
        (str): Data in Bedfile format with matches

    """
    try:
        infasta = os.path.join(tempdir, "out.fasta")
        infasta_complement = os.path.join(tempdir, "out_complement.fasta")
        parameters = ["seqkit",
                       "locate",
                       "-p", pamseq,
                       infasta,"-P",
                       "--bed",
                        "--threads", str(threads),
                        "-d"]
        if strand == "reverse":
            parameters[4] =infasta_complement
        p = subprocess.Popen(parameters, stdout=subprocess.PIPE)
        out, err = p.communicate()
        out = out.decode('utf-8')
        return out
    except subprocess.CalledProcessError as e:
        logging.error(e)
        raise e
    except FileNotFoundError as f:
        logging.error(f)
        raise f


def get_target(tempdir, mappingdata, targetlength, strand):
    """Given bedfile of PAM locations, function goes up or down and finds target

    Args:
        tempdir (str): Temporary directory
        mappingdata (str): Bedfile output from map_pam
        targetlength ...
        strand ...

    Returns:
        (str): Bedfile format with matches

    """
    target_dict = {}
    # track keys so that any duplicated entry can be removed from the final dictionary
    keys_list =[]
    # this won't work for big genomes because it reads into memory try seqio index
    infasta = SeqIO.read(os.path.join(tempdir, "out.fasta"), "fasta")
    infasta_complement = SeqIO.read(os.path.join(tempdir, "out_complement.fasta"), "fasta")
    bylines = mappingdata.splitlines()
    for entry in bylines:
        tline = entry.split()
        pam_sp = int(tline[1]) - 1 # -1 to adjust- because seqkit convention - starts from 1 but in python starts from 0.
        pam_ep = int(tline[2])
        pam_seq = tline[3]
        seqid = infasta.id
        # note the strand is not mean + from seqkit mapping. Comes from user- orientation of genome to search for target
        if strand=="forward":
            target_sp = pam_sp - targetlength
            target_ep = pam_sp
            target_seq = str(infasta.seq)[target_sp:target_ep]
        if strand=="reverse":
            target_sp = pam_ep
            target_ep = pam_ep + targetlength
            target_seq = str(infasta_complement.seq)[target_sp:target_ep]
        if len(target_seq) == targetlength:
                target_dict[target_seq]= {"seqid": seqid, "target_sp": target_sp,
                "target_ep": target_ep, "pam_seq": pam_seq,
                 "strand": strand}
        keys_list.append(target_seq)
    remove_target = [k for k, v in Counter(keys_list).items() if v > 1] # list of keys with more than one observation
    target_dict2 = {k: v for k, v in target_dict.items() if k not in remove_target} # although dict over writes on non-unique key, but we want to complete remove such observation
    return target_dict2


# parse key then create first section and remaining part, also take care of strand specificity
def parse_target(targetdict, strand, seqlengthtopam): # use local variable for close12
    """Given a dictionary of target sequence, parse target sequence into two parts:
    close region and remaining seq, then create a new dictionary with unique close region sequences as keys

        Args:
            targetdict (dict): Dictionary of target sequences obtained after running get_target

        Returns:
            parse_target_dict(dict): Dictionary with unique close region sequences as keys

    """
    parse_target_dict={}
    # track keys so that any duplicated entry can be removed from the final dictionary
    keys_list =[]
    for items in targetdict.items():
        if items[1]['strand']=="forward":
            proxitopam = items[0][-seqlengthtopam:]
            distaltopam = items[0][:-seqlengthtopam]
            items[1]['target'] = items[0] # move target sequence (key) as value
            items[1]['distaltopam'] = distaltopam
            items[1]['proxitopam'] = proxitopam
            parse_target_dict[proxitopam] = items[1] ## will retain only target with unique bp, make proxitopam as key
            keys_list.append(proxitopam)
        if strand == "reverse":
            proxitopam = items[0][:seqlengthtopam]
            distaltopam = items[0][seqlengthtopam:]
            items[1]['target'] = items[0]
            items[1]['distaltopam'] = distaltopam
            items[1]['proxitopam'] = proxitopam
            parse_target_dict[proxitopam] = items[1]
            keys_list.append(proxitopam)
    remove_target = [k for k, v in Counter(keys_list).items() if v > 1]
    parse_target_dict2 = {k: v for k, v in parse_target_dict.items() if k not in remove_target}
    return parse_target_dict2

#nms lib - create a index with remainingseq
def create_index(strings):
    """Initializes and returns a NMSLIB index
    Args:
        strings (str): Strings to calculate distance

    Returns:
        index (obj): Returns a NMSLIB index
    """
    index = nmslib.init(space='leven',
                        dtype=nmslib.DistType.INT,
                        data_type=nmslib.DataType.OBJECT_AS_STRING,
                        method='small_world_rand')
    index.addDataPointBatch(strings)
    index.createIndex(print_progress=True)
    return index

## Calculate Levenshtein distance among remainingseq, and remove any remainingseq that are similar
def filter_parse_target(parse_dict, threads=1, levendistance=2):
    """Returns filtered target sequences based on Leven distance (greater than 2) on sequences 12 bp away from the PAM motif
    Args:
        parse_dict(dict): A dictionary with parse target sequence
    Returns:
        filter_pasrse_dict(dict): A dictionary whose target sequences that are 12 bp away from PAM sequences are at the Levenshtein distance greater than 2.
    """
    filter_parse_dict={}
    # get a list of ramaning sequences
    rms_list=[]
    for key_val in parse_dict.keys():
        rms_list.append(parse_dict[key_val]['distaltopam'])
    # initialize a new index
    ref_index = create_index(rms_list)
    # Find knn of 1 for each remaning seq
    for keys, value in parse_dict.items():
        query_string = value['distaltopam']
        ids, distances = ref_index.knnQuery(query_string, k=2) ## k =number of k nearest neighbours (knn)
        if distances[1] > levendistance: # check the second value, because the first value will be mostly zero, distance between the same query and index
            filter_parse_dict[keys] = value
    return filter_parse_dict


def get_genbank_features(inputgbk):
    ## Pseudocode
    """Return a list of features for a genbank file

    Args:
        genbank (genebank): Genbank file to process


    Returns:
        (list): List of features with genebank informations
    """
    feature_list = []
    genebank_file = SeqIO.parse(inputgbk,"genbank")
    for entry in genebank_file:
        for record in entry.features:
            feature_dict = {}
            if record.type in ['CDS', 'gene']:
                feature_dict["accession"] = entry.id
                feature_dict["start"] = record.location.start.position
                feature_dict["stop"] = record.location.end.position
                feature_dict["type"] = record.type
                feature_dict["strand_for_feature"] = 'reverse' if record.strand < 0 else 'forward'
                for qualifier_key, qualifier_val in record.qualifiers.items():
                    feature_dict[qualifier_key] = qualifier_val
                feature_list.append(feature_dict)
    return feature_list


# ######### pybedtools ########

def get_nearby_feature(target_mappingfile, featurefile):
    """Adds downstream information to the given target sequences and mapping information

    Args:
        (tsv): mapping file with target sequence

    Returns:
        (tsv): A file with target sequences, mapping information, and downstream information
    """
    # format to featurefile
    featurebed = BedTool(featurefile.splitlines())
    # format to mapping file
    mapbed = BedTool(target_mappingfile.splitlines())
    # get feature downstream of target sequence
    downstream = mapbed.closest(featurebed , d=True, fd=True, D="a", t="first")
    # get feature upstream of target sequence
    upstream = mapbed.closest(featurebed , d=True, id=True, D="a", t="first")
    return downstream, upstream


def merge_downstream_upstream(downsfile,upsfile,columns_name, outputfilename):
    """Return a merged file

    Args:
        (tsv): A file with target sequences, mapping information, and upstream information
        (tsv): A file with target sequences, mapping information, and downstream information

    Returns:
        (dataframe): A DataFrame with merged upstream information and downstream information for a target sequence
    """
    downstream_df = downsfile.to_dataframe(names=columns_name,low_memory=False)
    upstream_df = upsfile.to_dataframe(names=columns_name,low_memory=False)
    all_df = pd.merge(downstream_df, upstream_df,
                      right_on=columns_name[:8],
                      left_on=columns_name[:8],
                      how='outer')
    all_df.to_csv(os.path.join(os.getcwd(),outputfilename),index=False,sep='\t',header=True)
    return all_df
################
def main(args=None):
    """Run Complete predictPAM workflow.
    """
    # Set up logging
    parser = myparser()
    if not args:
        args = parser.parse_args()

    _logger_setup(args.log)
    try:
        pamseq = Seq(args.pamseq, IUPAC.unambiguous_dna)
        #parse genbank file: todo allow multiple Genbank files
        #record = SeqIO.parse(args.gbkfile, "genbank")

        if args.tempdir:
            if not os.path.exists(args.tempdir):
                logging.warning("Specified location for tempfile ({}) does not exist, using default location.".format(tempdir))
                tempdir = tempfile.mkdtemp(prefix='pamPredict_')
        else:
            tempdir = tempfile.mkdtemp(prefix='pamPredict_', dir=args.tempdir)
        logging.info("Temp directory is: %s", tempdir)
        
        
        # Try to avoid writing out and reading genome in fasta format
        logging.info("Retriving fastas- forward and reverse from a genbanke file")
        get_fastas(genbank=args.gbkfile, tempdir=tempdir)
        print(tempdir)
        
        # mapping
        logging.info("Mapping pam to the genome")
        mapfile = map_pam(tempdir=tempdir, pamseq=args.pamseq, threads=args.threads, strand=args.strand)

        # Retrieving target sequence
        logging.info("Retrieving target sequence for matching PAM : %s", tempdir)
        targetdict = get_target(tempdir=tempdir, mappingdata=mapfile, targetlength=args.targetlength, strand=args.strand)

        # Parsing target sequence into two: 1)close12 and 2) remainingseq
        logging.info("Parsing target sequence into two: 1)proxitopam and 2) distaltopam")
        parsetargetdict = parse_target(targetdict, strand=args.strand, seqlengthtopam=args.lcp)

        # Calculate Levenshtein distance among remainingseq, and remove any remainingseq that are similar
        logging.info("Filtering parse target sequence based on Levenshtein distance using NMSLIB index, with knn=1")
        filterparsetargetdict = filter_parse_target(parsetargetdict, threads=args.threads, levendistance=args.eds)

        # reformating filterparsetargetdict- to make compatible for pybed
        filterparsetargetdict_pd = pd.DataFrame.from_dict(filterparsetargetdict, orient='index')
        # remove index, which is key of dict
        filterparsetargetdict_pd_dindx = filterparsetargetdict_pd.reset_index(drop=True)
        # pybed takes tab separated file with no header, plus first three column has to be as above
        filterparsetargetdict_pd_dindx_tab = filterparsetargetdict_pd_dindx.to_csv(index=False,sep='\t',header=False)

        # get get_genbank_features from a genebank file
        logging.info("Retrieving CDS/gene information for each record in the genebank file")
        genebankfeatures = get_genbank_features(args.gbkfile)
        # reformating genebankfeatures- to make compatible for pybed
        genebankfeatures_df = pd.DataFrame(genebankfeatures)
        # enpty tab crates isses in runnign pybed, so replance NaN with NA, then make tab separated
        genebankfeatures_df = genebankfeatures_df.replace(np.nan, "NA")
        genebankfeatures_df_tab = genebankfeatures_df.to_csv(index=False,sep='\t',header=False)

        # pybedtools
        logging.info("Mapping features downstream and upstream of target sequence")
        down, up = get_nearby_feature(filterparsetargetdict_pd_dindx_tab, genebankfeatures_df_tab)

        ### Adding column name to upstream and downstream output from pybed.
        # get columns from two input file: target_mappingfile(filterparsetargetdict), featurefile(genebankfeatures_df)
        targetfile_columns = list(list(filterparsetargetdict.values())[0].keys())
        featurefile_columns = list(genebankfeatures_df.columns) # from genebank feature dataframe
        joined_columns = targetfile_columns + featurefile_columns
        joined_columns.append("distance") # on top of mapping upstream and downstream, we are recording distance in pybed closest. Thus need an extra column

        # merge upstream and downstream output
        logging.info("Merging downstream and upstream features.Columns with suffix _x represents information from downstream whereas suffix with _y represent that of upstream")
        merged_down_ups = merge_downstream_upstream(down,up,joined_columns,outputfilename=args.outfile)

    except Exception as e:
        logging.error("predictPAM terminated with errors. See the log file for details.")
        logging.error(e)
        raise SystemExit(1)
    finally:
        try:
            if not args.keeptemp:
                shutil.rmtree(tempdir)
        except UnboundLocalError:
            pass
        except AttributeError:
            pass



if __name__ == '__main__':
    main()
