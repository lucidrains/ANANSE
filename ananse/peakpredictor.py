import itertools
import os
import re
import sys
from glob import glob
from tempfile import NamedTemporaryFile

import joblib
import networkx as nx
import numpy as np
import pandas as pd
import qnorm
from fluff.fluffio import load_heatmap_data
from genomepy import Genome
from gimmemotifs.moap import moap
from gimmemotifs.motif import read_motifs
from gimmemotifs.scanner import scan_regionfile_to_table
from loguru import logger
from pandas import HDFStore
from pyfaidx import FastaIndexingError
from scipy.stats import rankdata
from sklearn.preprocessing import minmax_scale, scale

from ananse.utils import load_tfs, load_regions, get_motif_factors, check_cores
from . import PACKAGE_DIR

# This motif file is not created by default
#   * f"{self.data_dir}/reference.factor.feather"

BLACKLIST_TFS = [
    "NO ORTHOLOGS FOUND",  # gimme motif2factors artifact
]


class PeakPredictor:
    atac_data = None
    histone_data = None
    factor_models = {}

    def __init__(
        self,
        reference=None,
        atac_bams=None,
        histone_bams=None,
        regions=None,
        genome="hg38",
        pfmfile=None,
        factors=None,
        pfmscorefile=None,
        ncore=4,
    ):
        if reference is None:
            reference = os.path.join(PACKAGE_DIR, "db", "default_reference")
        if not os.path.exists(reference):
            raise NotADirectoryError(f"Could not find {reference}")
        self.data_dir = reference

        if atac_bams is None and histone_bams is None:
            raise ValueError("Need either ATAC-seq or H3K27ac BAM file(s).")

        if genome is None:
            logger.warning("Assuming genome is hg38")
            genome = "hg38"
        self.genome = genome
        self.species = _get_species(genome)

        if pfmfile is None and self.species not in ["human", "mouse"]:
            warnings = [
                f"The genome '{genome}' is not recognized as human or mouse.",
                "If you do have another species, the motif file likely needs to be adapted.",
                "Currently mouse and human gene names are used to link motif to TFs.",
                "If your gene symbols are different, then you will need to create a new mapping",
                "and use the `-p` argument. For a possible method to do this, see here:",
                "https://gimmemotifs.readthedocs.io/en/stable/reference.html#command-gimme-motif2factors",
            ]
            for warn in warnings:
                logger.warning(warn)

        self.ncore = ncore
        self.pfmfile = pfmfile
        # load motifs, f2m and motif_graph
        self._load_motifs(factors=factors)
        # load regions, _motifs and region_type
        self._load_regions(regions, pfmscorefile)
        # load ATAC data
        if atac_bams is not None:
            self.load_atac(atac_bams, update_models=False)
        # load histone ChIP-seq data
        if histone_bams is not None:
            self.load_histone(histone_bams, update_models=False)

        self._set_model_type()

    def _load_motifs(self, indirect=True, factors=None):
        """Load motif-associated data.

        For now, only default motifs are supported.
        Will read factors associated to motifs, and generates a graph of
        related factors based on different factors binding to the same motif.
        This information is used to select the most appropriate TF model.

        Parameters
        ----------
        indirect : bool, optional
            Include TF-motif associations that are not curated, for instance
            based on ChIP-seq motif prediction, or binding inference. This will
            greatly increase TF coverage. By default True.
        """
        # load motifs
        if self.pfmfile is None:
            logger.info("Loading default motif file")
        else:
            logger.info(f"Loading specified motif file: {self.pfmfile}")
        self.motifs = read_motifs(self.pfmfile, as_dict=True)

        # load factor2motifs
        self.f2m = self._load_factor2motifs(
            pfmfile=self.pfmfile, indirect=indirect, factors=factors
        )
        n = len(self.f2m)
        logger.info(f"  Using motifs for {n} factor{'' if n == 1 else 's'}")

        # load jaccard motif graph
        self._jaccard_motif_graph(indirect, factors)

    def _load_factor2motifs(self, pfmfile=None, indirect=True, factors=None):
        motifs = read_motifs(pfmfile, as_dict=True)
        f2m = {}

        valid_factors = []
        if self.species == "human":
            valid_factors = _load_human_factors()

        for name, motif in motifs.items():
            for factor in get_motif_factors(motif, indirect=indirect):
                # filter for presence in factors
                if factors is not None and factor not in factors:
                    continue

                # TODO: this is temporary, while the motif database we use
                # not very clean...
                if self.species == "human":
                    factor = factor.upper()

                if self.species == "human" and factor not in valid_factors:
                    continue

                f2m.setdefault(factor, []).append(name)

        # remove blacklisted TFs
        for tf in BLACKLIST_TFS:
            if tf in f2m:
                del f2m[tf]

        if len(f2m) == 0:
            raise ValueError(
                "Zero factors remain after filtering the motif2factors.txt associated with pfmfile!"
            )
        return f2m

    def _jaccard_motif_graph(self, indirect, factors):
        """
        Create a graph of TF nodes where edges are the Jaccard index of the motifs that they bind to.
        For instance, if TF1 binds motif A and B and TF2 binds motif B and C,
        then the edge weight of TF1 to TF2 will be 1/3.

        Sets
        ----
        self.motif_graph : nx.Graph
        """
        # load the complete (unfiltered) factor2motifs
        if indirect is False or factors is not None:
            complete_f2m = self._load_factor2motifs(self.pfmfile)
        else:
            complete_f2m = self.f2m.copy()

        # convert motif lists to sets
        for k, v in complete_f2m.items():
            complete_f2m[k] = set(v)

        # if an alternative motif2factors is used, we can use the
        # jaccard index to link the user's TFs to
        # the orthologous TF models in the ananse reference database
        if self.pfmfile is not None:
            reference_orthologs = self._load_factor2motifs()
            for k, v in reference_orthologs.items():
                if k in complete_f2m:
                    complete_f2m[k].update(set(v))
                else:
                    complete_f2m[k] = set(v)

        # compute the jaccard index between each combination of TFs
        self.motif_graph = nx.Graph()
        combinations = set(itertools.combinations(complete_f2m.keys(), 2))
        for tf1, tf2 in combinations:
            i = len(complete_f2m[tf1].intersection(complete_f2m[tf2]))
            u = len(complete_f2m[tf1].union(complete_f2m[tf2]))
            if i and u:  # both > 0
                jaccard = i / u
                self.motif_graph.add_edge(tf1, tf2, weight=1 - jaccard)

    def _load_regions(self, regions=None, pfmscorefile=None):
        """
        loads
        - self.regions: a list of regions to work with
        - self._motifs: a pd.DataFrame with regions as index,
            motifs as columns, and motif scores as values
        - self.region_type: a str for internal stuff
        """
        if pfmscorefile is not None:
            # if we have a pre-scanned file,
            # use regions from the index (optionally filtered by regions)
            self.region_type = "custom"
            self._load_prescanned_motifs(pfmscorefile, regions)
            self.regions = list(self._motifs.index)
        elif regions is not None:
            # if we have custom regions we have to scan for motifs.
            self.region_type = "custom"
            self.regions = list(set(regions))
            self._scan_motifs(regions)
        else:
            # if the reference regions are used,
            # we can use existing data such as motif scores.
            self.region_type = "reference"
            self._load_reference_data()
        logger.info(f"  Using {len(self.regions)} regions")

    def _load_prescanned_motifs(self, pfmscorefile, regions=None):
        """
        Use pre-scanned gimmemotifs motif scores.

        Parameters
        ----------
        pfmscorefile : str/file
            pre-scanned gimmemotifs scores file

        regions : list, optional
            list of regions to filter the pfmscorefile by
        """
        logger.info("Loading pre-scanned motif scores")

        motif_df = pd.read_table(pfmscorefile, comment="#", index_col=0)

        if regions is not None:
            # warn if regions are not found in the pfmscorefile (likely a typo/later addition)
            invalid = set(regions) - set(motif_df.index)
            if len(invalid) > 0:
                logger.warning(f"{len(invalid)} regions not found in the pfmscorefile:")
                logger.warning(", ".join(list(invalid)))
                logger.warning(
                    "These regions are ignored. "
                    "Create a new pfmscorefile with these regions to include them!"
                )
                regions = list(set(regions) - invalid)

            # filter pfmscorefile and regions for overlap
            overlap = list(set(regions) & set(motif_df.index))
            if len(overlap) < len(motif_df.index):
                logger.debug(
                    f"Subsetting pfmscorefile to requested {len(regions)} regions"
                )
                motif_df = motif_df.loc[overlap]

        self._motifs = pd.DataFrame(index=motif_df.index)
        cols = motif_df.columns.to_list()
        for factor in self.f2m:
            motifs = self.f2m[factor]
            if not bool(set(motifs) & set(cols)):
                logger.warning(
                    f"No motifs for factor '{factor}' "
                    f"were found in pfmscorefile '{os.path.basename(pfmscorefile)}'"
                )
                continue
            self._motifs[factor] = motif_df[motifs].mean(1)

    def _scan_motifs(self, regions, **kwargs):
        """[summary]

        Parameters
        ----------
        regions : [type]
            [description]
        """
        logger.info("Scanning regions for motifs")
        with NamedTemporaryFile("w") as regionfile:
            print("region", file=regionfile)
            for region in regions:
                print(region, file=regionfile)
            regionfile.flush()

            # only scan motifs for our factors
            motifs = list(self.motifs.values())
            # TODO: after gimme > 0.16.1, this code block can be removed (fixed in #d088778)
            tmp = NamedTemporaryFile(mode="w", delete=False)
            for m in motifs:
                tmp.write("{}\n".format(m.to_pwm()))
            tmp.close()
            motifs = tmp.name
            # TODO: end of code block
            motif_df = scan_regionfile_to_table(
                regionfile.name,
                self.genome,
                scoring="score",
                pfmfile=motifs,
                ncpus=self.ncore,
                **kwargs,
            )
            self._motifs = pd.DataFrame(index=motif_df.index)
            for factor in self.f2m:
                self._motifs[factor] = motif_df[self.f2m[factor]].mean(1)

    def _load_reference_data(self):
        """Load data for reference regions.

        Will load three types of data:
        * Motif scores.
        * The average peak coverage (self._avg)
        * The distance from the peak to nearest TSS. (self._dist)

        All of these data are only used with the reference set of regions.
        """
        # Read motifs
        logger.info("Loading motifs for reference")
        fpath = os.path.join(self.data_dir, "reference.factor.feather")
        if not os.path.exists(fpath):
            raise FileNotFoundError(
                f"{fpath} not found. For hg38, download the REMAP "
                "reference dataset and specify it location with -R, "
                "or (for any species) specify regions with -r"
            )
        self._motifs = pd.read_feather(fpath)
        self._motifs.set_index(self._motifs.columns[0], inplace=True)

        # Read average coverage
        logger.info("Loading average peak coverage for reference")
        self._avg = pd.read_table(
            f"{self.data_dir}/reference.coverage.txt",
            sep="\t",
            comment="#",
            index_col=0,
        )
        self._avg.columns = ["average"]
        self._avg["average"] = self._avg["average"] / self._avg["average"].max()

        # Read distance to TSS
        logger.info("Loading distance for reference")
        self._dist = pd.read_table(
            f"{self.data_dir}/reference.dist_to_tss.txt",
            sep="\t",
            comment="#",
            index_col=0,
        )

        # Set regions
        self.regions = list(self._avg.index)

    def _load_bams(self, bams, title, window=200):
        tmp = pd.DataFrame(index=self.regions)
        n_regions = len(self.regions)
        with NamedTemporaryFile(mode="w") as f_out:

            for region in self.regions:
                print("{}\t{}\t{}".format(*re.split("[:-]", region)), file=f_out)
            f_out.flush()

            for bam in bams:
                name, regions, r_data, _ = load_heatmap_data(
                    f_out.name,
                    bam,
                    bins=1,
                    up=window // 2,
                    down=window // 2,
                    rmdup=True,
                    rmrepeats=True,
                )
                if len(r_data.T[0]) == n_regions:
                    tmp[name] = r_data.T[0]
                else:
                    # "regions" contains overlap, "r_data.T[0]" contains scores (order is identical)
                    df = pd.DataFrame(
                        np.array(regions)[:, 0:3],
                        columns=["chrom", "start", "end"],
                        dtype=str,
                    )
                    df["region"] = df["chrom"] + ":" + df["start"] + "-" + df["end"]
                    df[name] = r_data.T[0]
                    df = df[["region", name]].set_index("region")

                    # merge on regions to get scores for overlapping regions (set rest to 0)
                    tmp = tmp.join(df)
                    tmp[name].fillna(0.0, inplace=True)

        fname = f"{self.data_dir}/{title}.qnorm.ref.txt.gz"
        if os.path.exists(fname):
            logger.debug(f"Quantile normalization for {title}")
            qnorm_ref = pd.read_table(fname, index_col=0)["qnorm_ref"].values
            if len(self.regions) != len(qnorm_ref):
                qnorm_ref = np.random.choice(
                    qnorm_ref, size=len(self.regions), replace=True
                )

            tmp = qnorm.quantile_normalize(tmp, target=qnorm_ref)
        else:
            tmp = np.log1p(tmp)

        # Limit memory usage by using float16
        tmp = tmp.mean(1).astype("float16").to_frame(title)

        fname = f"{self.data_dir}/{title}.mean.ref.txt.gz"
        if self.region_type == "reference" and os.path.exists(fname):
            mean_ref = pd.read_table(fname, index_col=0)
            if mean_ref.shape[0] == tmp.shape[0]:
                mean_ref.index = tmp.index
                tmp[f"{title}.relative"] = (
                    tmp[title] - mean_ref.loc[tmp.index]["mean_ref"].values
                )
                tmp[f"{title}.relative"] = scale(tmp[f"{title}.relative"])
            else:
                logger.debug(f"Regions of {fname} are not the same as input regions.")
                logger.debug("Skipping calculation of relative values.")

        tmp[title] = tmp[title] / tmp[title].max()

        return tmp

    def load_atac(self, bams, update_models=True):
        """Load ATAC-seq counts from BAM files.

        Parameters
        ----------
        bams : list
            List of file names.
        update_models : bool, optional
            Update the model used if data is loaded, by default True.
        """
        logger.info("Loading ATAC data")
        self.atac_data = self._load_bams(bams, title="ATAC", window=200)
        if update_models:
            self._set_model_type()

    def load_histone(self, bams, update_models=True):
        """Load H3K27ac ChIP-seq counts from BAM files.

        Parameters
        ----------
        bams : list
            List of file names.
        update_models : bool, optional
            Update the model used if data is loaded, by default True.
        """
        logger.info("Loading H3K27ac data")
        self.histone_data = self._load_bams(bams, title="H3K27ac", window=2000)
        if update_models:
            self._set_model_type()

    def _set_model_type(self):
        """Select the mode to use for binding prediction.

        Basically, this will select the columns that are available,
        based on the different types of data that are loaded.
        Reference regions will have the most information.
        """
        cols = ["motif"]
        if self.atac_data is not None:
            cols += ["ATAC"]
            if self.region_type == "reference":
                cols += ["ATAC.relative"]
        if self.histone_data is not None:
            cols += ["H3K27ac"]
        if self.region_type == "reference":
            cols += ["average", "dist"]
        cols = sorted(cols)
        self._X_columns = cols
        self._model_type = "_".join(cols)

        # Load models
        logger.info("Loading models")
        for fname in glob(os.path.join(self.data_dir, self._model_type, "*.pkl")):
            factor = fname.split("/")[-1].replace(".pkl", "")
            self.factor_models[factor] = joblib.load(fname)
        logger.info(f"  Using {len(self.factor_models)} models")

    def factors(self):
        """return a list of factors based on the f2m dict"""
        if self.species == "human":
            valid_factors = _load_human_factors()
            return [f for f in self.f2m if f in valid_factors]
        if self.species == "mouse":
            # Mouse mappings are included in the default motif db.
            # Using the fact here that mouse names are not all upper-case.
            # TODO: replace with a curated set of factors.
            return [f for f in self.f2m if f[1:].islower()]
        return list(self.f2m.keys())

    def predict_proba(self, factor=None, motifs=None, jaccard_cutoff=0.0):
        """Predict binding probability.

        Predict binding probability for either a TF (factor) or a set of
        motifs. Prediction will be based on the data that been loaded,
        either ATAC-seq or H3K27ac data or both.

        Parameters
        ----------
        factor : str, optional
            Transcription factor name.
        motifs : [type], optional
            Motifs. Currently not implemented.
        jaccard_cutoff : float, optional
            Cutoff for the minimum jaccard overlap between motifs of two TFs for them to be considered related.
            Related TFs can share models. Default = 0 (0.1 seems to work well based on subjective testing).
        Returns
        -------
        pandas.DataFrame
            DataFrame with binding probabilities
        """
        if factor is None and motifs is None:
            raise ValueError("Need either a TF name or one or more motifs.")

        if motifs is not None:
            raise NotImplementedError("Custom motifs not yet implemented!")

        if factor not in self.f2m:
            raise ValueError(f"Motif not known for {factor}")

        model, factor = self._load_model(factor, jaccard_cutoff)

        x = self._load_data(factor)
        proba = model.predict_proba(x)[:, 1]

        return pd.DataFrame(proba, index=self.regions)

    def _load_data(self, factor):
        tmp = pd.DataFrame({"motif": self._motifs[factor]}, index=self.regions)
        if self.atac_data is not None:
            tmp = tmp.join(self.atac_data)
        if self.histone_data is not None:
            tmp = tmp.join(self.histone_data)

        if self.region_type == "reference":
            tmp = tmp.join(self._avg)
            tmp = tmp.join(self._dist)
        tmp = tmp.dropna()
        return tmp[self._X_columns]

    def _load_model(self, factor, jaccard_cutoff=0.0):
        """
        Load TF-binding model that is:
        1. trained for that specific TF
        2. trained on a different TF, but with a motif overlap (jaccard similarity) larger than the cutoff
        3. a general TF binding model if the other options are not available

        Parameters
        ----------
        factor : str
            Transcription factor name.
        jaccard_cutoff : float, optional
            minimum jaccard similarity score that is needed to use the model of TF1 for TF2.
            0: any shared motifs, 1: all motifs shared. Default is 0.
        """
        model = None
        # 1. trained for that specific TF
        if factor in self.factor_models:
            logger.info(f"Using {factor} model")
            model = self.factor_models[factor]

        # 2. trained on a different TF, but with a motif jaccard index larger than the cutoff
        elif factor in self.motif_graph:
            # scores are inverted due to nx's cutoff method
            tfs = nx.single_source_dijkstra_path_length(
                self.motif_graph, factor, 1 - jaccard_cutoff
            )
            # tfs are sorted best>worst. Use the first one with a trained model
            for tf in tfs:
                if tf in self.factor_models:
                    ji = round(1 - tfs[tf], 2)
                    logger.info(f"Using {tf} model for {factor} (jaccard index {ji})")
                    model = self.factor_models[tf]
                    break

        # 3. a general TF binding model if the other options are not available
        if model is None:
            logger.info(f"Using general model for {factor} (no related TF found)")
            model = self.factor_models["general"]

        return model, factor

    def predict_factor_activity(self, nregions=20_000):
        """Predict TF activity.

        Predicted based on motif activity using ridge regression.

        Parameters
        ----------
        """
        # Run ridge regression using motif score to predict (relative) ATAC/H3K27ac signal
        try:
            nregions = int(nregions)
        except ValueError:
            logger.warning("nregions is not an integer, using default number of 20_000")
            nregions = 20_000

        activity = pd.DataFrame()
        for df in (self.atac_data, self.histone_data):
            if df is None:
                continue

            for col in df.columns:
                with NamedTemporaryFile() as f:
                    # float16 will give NaN's
                    signal = df[col].astype("float32")
                    signal = pd.DataFrame({col: scale(signal)}, index=df.index)
                    if df.shape[0] < nregions:
                        signal.to_csv(f.name, sep="\t")
                    else:
                        signal.sample(nregions).to_csv(f.name, sep="\t")
                    try:
                        activity = activity.join(
                            moap(
                                f.name,
                                genome=self.genome,
                                method="bayesianridge",
                                pfmfile=self.pfmfile,
                            ),
                            how="outer",
                        )
                    except Exception as e:
                        print(e)

        # Rank aggregation
        for col in activity:
            activity[col] = rankdata(activity[col])
        activity = activity.mean(1)
        activity[:] = minmax_scale(activity)

        # Take the maximum activity from the motifs of each factor
        factor_activity = []
        for factor, motifs in self.f2m.items():
            act = activity.loc[motifs].max()
            factor_activity.append([factor, act])

        factor_activity = pd.DataFrame(factor_activity, columns=["factor", "activity"])

        return factor_activity


def _get_species(genome):
    """returns 'human', 'mouse' or None"""
    # Try to get taxonomy id for genomepy managed genome.
    # If there is a taxonomy id, we can be really sure about the species.
    # If genome doesn't have a tax_id, then it will be 'na'.
    try:
        tax_id = Genome(genome).tax_id
        species = None
        if tax_id == 9606:
            species = "human"
        elif tax_id == 10090:
            species = "mouse"
        if isinstance(tax_id, int):
            # tax_id converts to int so it is valid, must be not human or mouse
            return species
    except (FileNotFoundError, FastaIndexingError):
        pass

    # backup: try to get the species from the filename
    mapping = {
        "hg38": "human",
        "hg19": "human",
        "GRCh3": "human",
        "mm10": "mouse",
        "mm9": "mouse",
        "GRCm3": "mouse",
    }
    base_genome = os.path.basename(genome.strip("/"))
    for name, species in mapping.items():
        if name in base_genome:
            return species


def _load_human_factors():
    tf_xlsx = os.path.join(PACKAGE_DIR, "db", "lovering.tfs.xlsx")
    valid_factors = pd.read_excel(
        tf_xlsx,
        engine="openpyxl",
        sheet_name=1,
    )
    valid_factors = valid_factors.loc[
        valid_factors["Pseudogene"].isnull(), "HGNC approved gene symbol"
    ].values
    valid_factors = list(set(valid_factors) - {"EP300"})
    return valid_factors


def _check_input_files(*args):
    files = []
    for arg in args:
        if arg is None:
            continue
        if isinstance(arg, list):
            files.extend(arg)
        else:
            files.append(arg)

    all_files_found = True
    for fname in files:
        if not os.path.exists(fname):
            logger.exception(f"Could not find {fname}!")
            all_files_found = False

    if not all_files_found:
        sys.exit(1)


def predict_peaks(
    outdir,
    atac_bams=None,
    histone_bams=None,
    regions=None,
    reference=None,
    factors=None,
    genome=None,
    pfmfile=None,
    pfmscorefile=None,
    jaccard_cutoff=0.0,
    ncore=4,
):
    """Predict binding in a set of genomic regions.

    Binding is predicted based on ATAC-seq and/or H3K27ac ChIP-seq data in
    combination with motif scores. The model that is used is flexible, based
    on the input data. The most accurate model will be the one that uses the
    references regions in combination with both ATAC-seq and H3K27ac ChIP-seq.

    The result will will be saved to an outputfile called `binding.tsv` in the
    output directory, specified by the `outdir` argument. This file wil contain
    three columns: factor, enhancer and binding. The binding columns represents
    the binding probability.

    To predict binding, `predict_peaks()` needs a set of input regions. For
    human, you have two options. You can either use the reference set of
    putative enhancer regions, as described in the ANANSE manuscript [1]. This
    is specified by the `reference` argument.
    Alternatively, you can specify one or more region files with the
    `regionfiles` argument. These are files in BED or narrowPeak format, that
    describe potential enhancers. For instance, a reference enhancer set, peaks
    from your ATAC-seq experiments or any other collection of regions. For
    accurate motif analysis, these should be as precise as possible. BroadPeaks
    from histone ChIP-seq are not really suitable. NarrowPeaks from ATAC-seq,
    DNase-seq or TF ChIP-seq will be fine.

    Parameters
    ----------
    outdir : str
        Name of output directory.
    atac_bams : list, optional
        List of BAM files, by default None
    histone_bams : list, optional
        List of H3K27ac ChIP-seq BAM files, by default None
    regions : str or list, optional
        BED file or text file with regions, or a list of BED, narrowPeak or
        broadPeak files If None, then the reference regions are used.
    reference : str, optional
        Directory name to a reference.
    factors : list, optional
        List of TF names or file with TFs, one per line. If None (default),
        then all TFs are used.
    genome : str, optional
        Genome name. The default is hg38.
    pfmfile : str, optional
        Motifs in PFM format, with associated motif2factors.txt file.
    pfmscorefile : str, optional
        Path to file with pre-scanned motif scores.
    jaccard_cutoff : int, optional
        minimum jaccard similarity score that is needed to use the model of TF1 for TF2.
        0: any shared motifs, 1: all motifs shared. Default is 0.
    ncore : int, optional
        Number of threads to use. Default is 4.
    """
    if reference is None and regions is None and pfmscorefile is None:
        warnings = [
            "Need either 1) a `reference` directory, 2) one or more `regionfiles`, "
            "or 3) a `pfmscorefile` (a set of pre-scanned regions)!",
            "1) For human data, you can download the REMAP reference here: "
            "https://doi.org/10.5281/zenodo.4768075",
            "2) As regionfiles you can specify one or more BED/narrowPeak files "
            "(with potential enhancer regions, for instance, all ATAC-seq peaks "
            "from your combined experiments)",
            "3) To get pre-scanned regions, run "
            "`gimme scan -Tz --gc -g GENOME REGIONFILES > pfmscorefile.tsv` "
            "(nice if you plan to reuse these regions)",
            "Please see the docs for more info on these options.",
        ]
        for warn in warnings:
            logger.error(warn)
        sys.exit(1)

    if reference is not None and (regions is not None or pfmscorefile is not None):
        logger.error(
            "Need either a reference directory *or* a set of input regions"
            "/pfmscorefile (pfmscorefile and regionfiles can be combined)"
        )
        sys.exit(1)

    # Check if all specified BAM files exist
    _check_input_files(atac_bams, histone_bams)

    # Check genome, will fail if it is not a correct genome name or file
    Genome(genome)

    if reference is not None:
        if not os.path.exists(reference):
            logger.error(f"Reference directory {reference} does not exist!")
            sys.exit(1)

    ncore = check_cores(ncore)

    if not os.path.exists(outdir):
        os.makedirs(outdir, exist_ok=True)

    # Read the factors, from a file if needed
    factors = load_tfs(factors)

    # If regions are specified, read them in, combining multiple files if necessary.
    regions = load_regions(regions, genome, outdir)

    p = PeakPredictor(
        reference=reference,
        atac_bams=atac_bams,
        histone_bams=histone_bams,
        regions=regions,
        genome=genome,
        pfmfile=pfmfile,
        factors=factors,
        pfmscorefile=pfmscorefile,
        ncore=ncore,
    )

    outfile = os.path.join(outdir, "binding.h5")
    # Make sure we create a new file
    with open(outfile, "w"):
        pass

    with HDFStore(outfile, complib="lzo", complevel=9) as hdf:

        if p.atac_data is not None:
            hdf.put(key="_atac", value=p.atac_data, format="table")

        if p.histone_data is not None:
            hdf.put(key="_h3k27ac", value=p.histone_data, format="table")

        logger.info("Predicting TF activity")
        factor_activity = p.predict_factor_activity()
        hdf.put(key="_factor_activity", value=factor_activity, format="table")

        logger.info("Predicting binding per TF:")
        proba = None
        for factor in p.factors():
            try:
                proba = p.predict_proba(factor, jaccard_cutoff=jaccard_cutoff)
                hdf.put(
                    key=f"{factor}",
                    value=proba.iloc[:, -1].reset_index(drop=True).astype(np.float16),
                    format="table",
                )
            except ValueError as e:
                logger.debug(str(e))

        if proba is None:
            # PeakPredictor.predict_proba() went wrong for all TFs
            logger.error(
                "Something went wrong! Please let us know by raising an issue with "
                "your ANANSE version and the log output at "
                "https://github.com/vanheeringen-lab/ANANSE/issues"
            )
            sys.exit(1)
        hdf.put(key="_index", value=proba.index.to_series(), format="table")
