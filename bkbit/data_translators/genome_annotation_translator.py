import re
import hashlib
import tempfile
import uuid
import logging
import urllib
import urllib.request
from urllib.parse import urlparse
import os
import json
from datetime import datetime
from collections import defaultdict
import subprocess
import gzip
from tqdm import tqdm
import click


from bkbit.models import genome_annotation as ga


logging.basicConfig(
    filename="gff3_translator_" + datetime.now().strftime("%Y-%m-%d_%H:%M:%S") + ".log",
    format="%(levelname)s: %(message)s (%(asctime)s)",
    datefmt="%m/%d/%Y %I:%M:%S %p",
    level=logging.DEBUG,
)
logger = logging.getLogger(__name__)


## CONSTANTS ##

TAXON_SCIENTIFIC_NAME = {
    "9606": "Homo sapiens",
    "10090": "Mus musculus",
    "9544": "Macaca mulatta",
    "9483": "Callithrix jacchus",
    "60711": "Chlorocebus sabaeus",
    "9361": "Dasypus novemcinctus",
    "9685": "Felis catus",
    "9669": "Mustela putorius furo",
    "30611": "Otolemur garnettii",
    "9593": "Gorilla gorilla",
    "13616": "Monodelphis domestica",
    "9823": "Sus scrofa",
    "9986": "Oryctolagus cuniculus",
    "10116": "Rattus norvegicus",
    "27679": "Saimiri boliviensis",
    "246437": "Tupaia belangeri chinensis",
    "9407": "Rousettus aegyptiacus",
    "9598": "Pan troglodytes"
}

SCIENTIFIC_NAME_TO_TAXONID = {
    "Homo sapiens": "9606",
    "Mus musculus": "10090",
    "Macaca mulatta": "9544",
    "Callithrix jacchus": "9483",
    "Chlorocebus sabaeus": "60711",
    "Dasypus novemcinctus": "9361",
    "Felis catus": "9685",
    "Mustela putorius furo": "9669",
    "Otolemur garnettii": "30611",
    "Gorilla gorilla": "9593",
    "Monodelphis domestica": "13616",
    "Sus scrofa": "9823",
    "Oryctolagus cuniculus": "9986",
    "Rattus norvegicus": "10116",
    "Saimiri boliviensis": "27679",
    "Tupaia belangeri chinensis": "246437",
    "Rousettus aegyptiacus": "9407",
    "Pan troglodytes": "9598"
}

TAXON_COMMON_NAME = {
    "9606": "human",
    "10090": "mouse",
    "9544": "rhesus macaque",
    "9483": "common marmoset",
    "60711": "green monkey",
    "9361": "nine-banded armadillo",
    "9685": "cat",
    "9669": "ferret",
    "30611": "galago",
    "9593": "gorilla",
    "13616":"gray short-tailed opossum",
    "9823": "pig",
    "9986": "rabbit",
    "10116": "rat",
    "27679": "squirrel monkey",
    "246437": "Chinese tree shrew",
    "9407": "egyptian fruit bat",
    "9598": "chimpanzee"
}

PREFIX_MAP = {
    "NCBITaxon": "http://purl.obolibrary.org/obo/NCBITaxon_",
    "NCBIGene": "http://identifiers.org/ncbigene/",
    "ENSEMBL": "http://identifiers.org/ensembl/",
    "NCBIAssembly": "https://www.ncbi.nlm.nih.gov/assembly/",
}
NCBI_GENE_ID_PREFIX = "NCBIGene"
ENSEMBL_GENE_ID_PREFIX = "ENSEMBL"
TAXON_PREFIX = "NCBITaxon"
ASSEMBLY_PREFIX = "NCBIAssembly"
BICAN_ANNOTATION_PREFIX = "bican:annotation-"
GENOME_ANNOTATION_DESCRIPTION_FORMAT = (
    "{authority} {taxon_scientific_name} Annotation Release {genome_version}"
)
DEFAULT_FEATURE_FILTER = ("gene", "pseudogene", "ncRNA_gene")
DEFAULT_HASH = ["MD5"]


class Gff3:
    def __init__(
        self,
        content_url,
        assembly_accession=None,
        assembly_strain=None,
        hash_functions=DEFAULT_HASH,
    ):
        """
        Initializes an instance of the GFFTranslator class.

        Parameters:
        - content_url (str): The URL of the GFF file.
        - assembly_id (str): The ID of the genome assembly.
        - assembly_strain (str, optional): The strain of the genome assembly. Defaults to None.
        - hash_functions (tuple[str]): A tuple of hash functions to use for generating checksums. Defaults to ('MD5').
        """
        self.logger = logger
        self.content_url = content_url
        
        ## STEP 1: Parse the content URL to get metadata
        # Parse content_url to get metadata
        url_metadata = self.parse_url()
        print(f'URL Metadata: {url_metadata}')
        if url_metadata is None:
            logger.critical(
                "The provided content URL is not supported. Please provide a valid URL."
            )
            raise ValueError(
                "The provided content URL is not supported. Please provide a valid URL."
            )
        
        # Define variables to store metadata
        taxon_id, assembly_id, assembly_version, assembly_label, genome_label, genome_version = None, None, None, None, None, None

        # Assign the authority type
        self.authority = url_metadata.get("authority")

        # Assign the taxon_id and assembly_id based on the authority
        if self.authority.value == ga.AuthorityType.NCBI.value:
            taxon_id = url_metadata.get("taxonid")
            assembly_id = url_metadata.get("assembly_accession")
        elif self.authority.value == ga.AuthorityType.ENSEMBL.value:
            self.taxon_id = SCIENTIFIC_NAME_TO_TAXONID.get(url_metadata.get("species").replace("_", " "))
            if assembly_accession is None:
                logger.critical(
                    "The assembly ID is required for Ensembl URLs. Please provide the assembly ID."
                )
                raise ValueError(
                    "The assembly ID is required for Ensembl URLs. Please provide the assembly ID."
                )
            assembly_id = assembly_accession

        # Assign assembly_version, assembly_label, genome_version, and genome_label
        assembly_version = assembly_id.split(".").get(1, None)
        assembly_label = url_metadata.get("assembly_name")
        genome_version = url_metadata.get("release_version")
        genome_label = self.authority.value + "-" + taxon_id + "-" + genome_version

        ## STEP 2: Download the GFF file 
        # Download the GFF file
        self.gff_file, hash_values = self.__download_gff_file()

        ## STEP 3: Generate the organism taxon, genome assembly, checksums, and genome annotation objects
        # Generate the organism taxon object
        self.organism_taxon = self.generate_organism_taxon(taxon_id)
        self.genome_assembly = self.generate_genome_assembly(
            assembly_id, assembly_version, assembly_label, assembly_strain
        )
        self.checksums = self.generate_digest(hash_values, hash_functions)
        self.genome_annotation = self.generate_genome_annotation(
            genome_label, genome_version
        )
        
        self.gene_annotations = {}

    def __download_gff_file(self):
        # """
        # Downloads a GFF file from the specified content URL, decompresses it, and returns the path to the temporary file.

        # Returns:
        #     str: The path to the temporary file containing the decompressed GFF data.
        # """
        # # Request the file and get its size
        # response = urllib.request.urlopen(self.content_url)
        # total_size = int(response.headers.get('content-length', 0))
        # block_size = 1024  # 1 Kilobyte

        # # Create a temporary file for the gzip data
        # with tempfile.NamedTemporaryFile(suffix=".gz", delete=False) as f_gzip:
        #     gzip_file_path = f_gzip.name
            
        #     # Create a progress bar
        #     progress_bar = tqdm(total=total_size, unit='iB', unit_scale=True, desc="Downloading GFF file")

        #     # Read the file in chunks and write to the temporary file
        #     while True:
        #         data = response.read(block_size)
        #         if not data:
        #             break
        #         f_gzip.write(data)
        #         progress_bar.update(len(data))

        #     progress_bar.close()

        # return gzip_file_path
        response = urllib.request.urlopen(self.content_url)
        total_size = int(response.headers.get('content-length', 0))
        block_size = 1024  # 1 Kilobyte

        # Create hash objects
        md5_hash = hashlib.md5()
        sha256_hash = hashlib.sha256()
        sha1_hash = hashlib.sha1()

        # Create a temporary file for the gzip data
        with tempfile.NamedTemporaryFile(suffix=".gz", delete=False) as f_gzip:
            gzip_file_path = f_gzip.name

            # Create a progress bar
            progress_bar = tqdm(total=total_size, unit='iB', unit_scale=True, desc="Downloading GFF file")

            # Read the file in chunks, write to the temporary file, and update the hash
            while True:
                data = response.read(block_size)
                if not data:
                    break
                f_gzip.write(data)
                md5_hash.update(data)
                sha256_hash.update(data)
                sha1_hash.update(data)
                progress_bar.update(len(data))

            progress_bar.close()

        # Return the path to the temporary file and the md5 hash
        return gzip_file_path, {"MD5": md5_hash.hexdigest(), "SHA256": sha256_hash.hexdigest(), "SHA1":sha1_hash.hexdigest()}
    
    def parse_url(self):
        # NCBI : [assembly accession.version]_[assembly name]_[content type].[optional format]
        # ENSEMBL :  <species>.<assembly>.<_version>.gff3.gz -> organism full name, assembly name, genome version
        # Define regex patterns for NCBI and Ensembl URLs
        ncbi_pattern = r'/genomes/all/annotation_releases/(\d+)(?:/(\d+))?/(GCF_\d+\.\d+)[_-]([^/]+)/(GCF_\d+\.\d+)[_-]([^/]+)_genomic\.gff\.gz'
        ensembl_pattern = r'/([^/]+)\.([^/]+)\.(\d+)\.gff3\.gz$'
        
        # Parse the URL to get the path
        parsed_url = urlparse(self.content_url)
        print(f'Parsed URL: {parsed_url}')
        path = parsed_url.path
        
        # Determine if the URL is from NCBI or Ensembl and extract information
        if 'ncbi' in parsed_url.netloc:
            print("Parsing NCBI URL")
            ncbi_match = re.search(ncbi_pattern, path)
            print(ncbi_match)
            if ncbi_match:
                print("MATCH NCBI FOUND")
                return {
                    "authority": ga.AuthorityType.NCBI,
                    "taxonid": ncbi_match.group(1),
                    "release_version": ncbi_match.group(2) if ncbi_match.group(2) else ncbi_match.group(4),
                    "assembly_accession": ncbi_match.group(3),
                    "assembly_name": ncbi_match.group(6),
                }
        
        elif 'ensembl' in parsed_url.netloc:
            ensembl_match = re.search(ensembl_pattern, path)
            print(ensembl_match)
            print("Parsing Ensembl URL")
            if ensembl_match:
                print("MATCH ENSEMBL FOUND")
                return {
                    "authority": ga.AuthorityType.ENSEMBL,
                    "species": ensembl_match.group(1),
                    "assembly_name": ensembl_match.group(2),
                    "release_version": ensembl_match.group(3)
                }
        
        # If no match is found, return None
        return None

    def generate_organism_taxon(self, taxon_id: str):
        """
        Generates an organism taxon object based on the provided taxon ID.

        Args:
            taxon_id (str): The taxon ID of the organism.

        Returns:
            ga.OrganismTaxon: The generated organism taxon object.
        """
        self.logger.debug("Generating organism taxon")
        return ga.OrganismTaxon(
            id=TAXON_PREFIX + ":" + taxon_id,
            full_name=TAXON_SCIENTIFIC_NAME[taxon_id],
            name=TAXON_COMMON_NAME[taxon_id],
            iri=PREFIX_MAP[TAXON_PREFIX] + taxon_id,
        )

    def assign_authority_type(self, authority: str):
        """
        Assigns the authority type based on the given authority string.

        Args:
            authority (str): The authority string to be assigned.

        Returns:
            ga.AuthorityType: The corresponding authority type.

        Raises:
            Exception: If the authority is not supported. Only NCBI and Ensembl authorities are supported.
        """
        self.logger.debug("Assigning authority type")
        if authority.upper() == ga.AuthorityType.NCBI.value:
            return ga.AuthorityType.NCBI
        if authority.upper() == ga.AuthorityType.ENSEMBL.value:
            return ga.AuthorityType.ENSEMBL
        logger.critical(
            "Authority %s is not supported. Please use NCBI or Ensembl.", authority
        )
        raise ValueError(
            f"Authority {self.authority} is not supported. Please use NCBI or Ensembl."
        )

    def generate_genome_assembly(
        self,
        assembly_id: str,
        assembly_version: str,
        assembly_label: str,
        assembly_strain: str = None,
    ):
        """
        Generate a genome assembly object.

        Parameters:
        assembly_id (str): The ID of the assembly.
        assembly_version (str): The version of the assembly.
        assembly_label (str): The label of the assembly.
        assembly_strain (str, optional): The strain of the assembly. Defaults to None.

        Returns:
        ga.GenomeAssembly: The generated genome assembly object.
        """
        self.logger.debug("Generating genome assembly")
        return ga.GenomeAssembly(
            id=ASSEMBLY_PREFIX + ":" + assembly_id,
            in_taxon=[self.organism_taxon.id],
            in_taxon_label=self.organism_taxon.full_name,
            version=assembly_version,
            name=assembly_label,
            strain=assembly_strain,
        )

    def generate_genome_annotation(self, genome_label: str, genome_version: str):
        """
        Generates a genome annotation object.

        Args:
            genome_label (str): The label of the genome.
            genome_version (str): The version of the genome.

        Returns:
            ga.GenomeAnnotation: The generated genome annotation.
        """
        self.logger.debug("Generating genome annotation")
        return ga.GenomeAnnotation(
            id=BICAN_ANNOTATION_PREFIX + genome_label.upper(),
            digest=[checksum.id for checksum in self.checksums],
            content_url=[self.content_url],
            reference_assembly=self.genome_assembly.id,
            version=genome_version,
            in_taxon=[self.organism_taxon.id],
            in_taxon_label=self.organism_taxon.full_name,
            description=GENOME_ANNOTATION_DESCRIPTION_FORMAT.format(
                authority=self.authority.value,
                taxon_scientific_name=self.organism_taxon.full_name,
                genome_version=genome_version,
            ),
            authority=self.authority,
        )

    def generate_digest(
        self,
        hash_values: dict,
        hash_functions: tuple[str] = DEFAULT_HASH, 
    ) -> list[ga.Checksum]:
        """
        Generates checksum digests for the GFF file using the specified hash functions.

        Args:
            hash_functions (list[str]): A list of hash functions to use for generating the digests.

        Returns:
            list[ga.Checksum]: A list of Checksum objects containing the generated digests.

        Raises:
            ValueError: If an unsupported hash algorithm is provided.

        """
        self.logger.debug("Generating checksum digests")
        checksums = []
        for hash_type in hash_functions:
            self.logger.debug("Generating checksum for %s", hash_type)
            # Generate a UUID version 4
            uuid_value = uuid.uuid4()

            # Construct a URN with the UUID
            urn = f"urn:uuid:{uuid_value}"
            hash_type = hash_type.strip().upper()
            # Create a Checksum object
            if hash_type == ga.DigestType.SHA256.name:
                self.logger.debug("Generating SHA256 digest")
                checksums.append(
                    ga.Checksum(
                        id=urn,
                        checksum_algorithm=ga.DigestType.SHA256,
                        value=hash_values.get("SHA256")
                    )
                )
            elif hash_type == ga.DigestType.MD5.name:
                self.logger.debug("Generating MD5 digest")
                checksums.append(
                    ga.Checksum(
                        id=urn, 
                        checksum_algorithm=ga.DigestType.MD5, 
                        value=hash_values.get("MD5")
                    )
                )
            elif hash_type == ga.DigestType.SHA1.name:
                self.logger.debug("Generating SHA1 digest")
                checksums.append(
                    ga.Checksum(
                        id=urn,
                        checksum_algorithm=ga.DigestType.SHA1, 
                        value= hash_values.get("SHA1")
                    )
                )
            else:
                logger.error(
                    "Hash algorithm %s is not supported. Please use SHA256, MD5, or SHA1.",
                    hash_type,
                )
        return checksums

    def __get_line_count(self, file_path):
        """
        Get the line count of a file.

        Args:
            file_path (str): The path to the file.

        Returns:
            int: The number of lines in the file.
        """

        result = subprocess.run(
            ["wc", "-l", file_path], stdout=subprocess.PIPE, check=True
        )  # If check is True and the exit code was non-zero, it raises a CalledProcessError. 
           # The CalledProcessError object will have the return code in the returncode attribute,
           # and output & stderr attributes if those streams were captured.
        output = result.stdout.decode().strip()
        line_count = int(output.split()[0])  # Extract the line count from the output
        return line_count

    def parse(self, feature_filter: tuple[str] = DEFAULT_FEATURE_FILTER):
        """
        Parses the GFF file and extracts gene annotations based on the provided feature filter.

        Args:
            feature_filter (tuple[str]): Tuple of feature types to include in the gene annotations.

        Raises:
            FileNotFoundError: If the GFF file does not exist.

        Returns:
            None
        """
        gff_file = self.gff_file
        if self.gff_file.endswith(".gz"):
            # Decompress the gzip file
            with gzip.open(self.gff_file, "rb") as f_in:
                # Create a temporary file to save the decompressed data
                with tempfile.NamedTemporaryFile(delete=False) as f_out:
                    # Copy the decompressed data to the temporary file
                    f_out.write(f_in.read())
                    gff_file = f_out.name

        if not os.path.isfile(gff_file):
            raise FileNotFoundError(f"File {gff_file} does not exist.")

        with open(gff_file, "r", encoding="utf-8") as file:
            curr_line_num = 1
            progress_bar = tqdm(
                total=self.__get_line_count(gff_file), desc="Parsing GFF3 File"
            )
            for line_raw in file:
                line_strip = line_raw.strip()
                if curr_line_num == 1 and not line_strip.startswith("##gff-version 3"):
                    logger.critical(
                        'Line %s: ##gff-version 3" missing from the first line.',
                        curr_line_num,
                    )
                elif len(line_strip) == 0:  # blank line
                    continue
                elif line_strip.startswith("##"):  # TODO: parse more metadata
                    pass
                elif line_strip.startswith("#"):  # TODO: parse more metadata
                    pass
                else:  # line may be a feature or unknown
                    tokens = list(map(str.strip, line_raw.split("\t")))
                    if len(tokens) != 9:
                        logger.warning(
                            "Line %s: Features are expected 9 columns, found %s.",
                            curr_line_num,
                            len(tokens),
                        )
                    if (
                        tokens[2] in feature_filter
                    ):  # only look at rows that have a type that is included in feature_filter
                        attributes = self.__merge_values(
                            tuple(a.split("=") for a in tokens[8].split(";"))
                        )
                        # TODO: Write cleaner code that calls respective generate function based on the authority automatically
                        if (
                            self.genome_annotation.authority
                            == ga.AuthorityType.ENSEMBL
                        ):
                            gene_annotation = self.generate_ensembl_gene_annotation(
                                attributes, curr_line_num
                            )
                            if gene_annotation is not None:
                                self.gene_annotations[gene_annotation] = gene_annotation
                        elif (
                            self.genome_annotation.authority
                            == ga.AuthorityType.NCBI
                        ):
                            gene_annotation = self.generate_ncbi_gene_annotation(
                                attributes, curr_line_num
                            )
                            if gene_annotation is not None:
                                self.gene_annotations[gene_annotation.id] = (
                                    gene_annotation
                                )
                progress_bar.update(1)
                curr_line_num += 1
            progress_bar.close()

    def generate_ensembl_gene_annotation(self, attributes, curr_line_num):
        """
        Generates a GeneAnnotation object for Ensembl based on the provided attributes.

        Args:
            attributes (dict): A dictionary containing the attributes of the gene.
            curr_line_num (int): The line number of the current row in the input file.

        Returns:
            GeneAnnotation or None: The generated GeneAnnotation object if it is not a duplicate,
            otherwise None.

        Raises:
            None

        """
        stable_id = self.__get_attribute(attributes, "gene_id", curr_line_num)
        if stable_id:
            stable_id = stable_id.split(".")[0]

        # Check and validate the name attribute
        name = self.__get_attribute(attributes, "Name", curr_line_num)

        # Check and validate the description attribute
        description = self.__get_attribute(attributes, "description", curr_line_num)

        # Check and validate the biotype attribute
        biotype = self.__get_attribute(attributes, "biotype", curr_line_num)

        gene_annotation = ga.GeneAnnotation(
            id=ENSEMBL_GENE_ID_PREFIX + ":" + stable_id,
            source_id=stable_id,
            symbol=name,
            name=name,
            description=description,
            molecular_type=biotype,
            referenced_in=self.genome_annotation.id,
            in_taxon=[self.organism_taxon.id],
            in_taxon_label=self.organism_taxon.full_name,
        )
        # handle duplicates
        if gene_annotation not in self.gene_annotations:
            return gene_annotation
        return None

    def generate_ncbi_gene_annotation(self, attributes, curr_line_num):
        """
        Generates a GeneAnnotation object for NCBI based on the provided attributes.

        Args:
            attributes (dict): A dictionary containing the attributes of the gene.
            curr_line_num (int): The line number of the current row in the input file.

        Returns:
            GeneAnnotation or None: The generated GeneAnnotation object if it is not a duplicate,
            otherwise None.

        Raises:
            None

        """
        stable_id = None
        if "Dbxref" in attributes:
            dbxref = {t.strip() for s in attributes["Dbxref"] for t in s.split(",")}
            geneid_values = set()
            for reference in dbxref:
                k, v = reference.split(":", 1)
                if k == "GeneID":
                    geneid_values.add(v.split(".")[0])
            if len(geneid_values) == 1:
                stable_id = geneid_values.pop()
        else:
            logger.error(
                "Line %s: No GeneAnnotation object created for this row due to missing dbxref attribute.",
                curr_line_num,
            )
            return None

        if not stable_id:
            logger.error(
                "Line %s: No GeneAnnotation object created for this row due to number of GeneIDs provided in dbxref attribute is not equal to one.",
                curr_line_num,
            )
            return None

        # Check and validate the name attribute
        name = self.__get_attribute(attributes, "Name", curr_line_num)

        # Check and validate the description attribute
        description = self.__get_attribute(attributes, "description", curr_line_num)

        # Check and validate the biotype attribute
        biotype = self.__get_attribute(attributes, "gene_biotype", curr_line_num)

        # Parse synonyms
        synonyms = []
        if "gene_synonym" in attributes:
            synonyms = list(
                {t.strip() for s in attributes["gene_synonym"] for t in s.split(",")}
            )
            synonyms.sort()  # note: this is not required, but it makes the output more predictable therefore easier to test
        else:
            logger.warning(
                "Line %s: synonym is not set for this row's GeneAnnotation object due to missing gene_synonym attribute.",
                curr_line_num,
            )

        gene_annotation = ga.GeneAnnotation(
            id=NCBI_GENE_ID_PREFIX + ":" + stable_id,
            source_id=stable_id,
            symbol=name,
            name=name,
            description=description,
            molecular_type=biotype,
            referenced_in=self.genome_annotation.id,
            in_taxon=[self.organism_taxon.id],
            in_taxon_label=self.organism_taxon.full_name,
            synonym=synonyms,
        )
        if gene_annotation.id in self.gene_annotations:
            if gene_annotation != self.gene_annotations[gene_annotation.id]:
                return self.__resolve_ncbi_gene_annotation(
                    gene_annotation, curr_line_num
                )
            if name != self.gene_annotations[gene_annotation.id].name:
                logger.warning(
                    "Line %s: GeneAnnotation object with id %s already exists with a different name. Current name: %s, Existing name: %s",
                    curr_line_num,
                    stable_id,
                    name,
                    self.gene_annotations[gene_annotation.id].name
                )
                return None
        return gene_annotation

    def __get_attribute(self, attributes, attribute_name, curr_line_num):
        """
        Get the value of a specific attribute from the given attributes dictionary.

        Args:
            attributes (dict): A dictionary containing attribute names and their values.
            attribute_name (str): The name of the attribute to retrieve.
            curr_line_num (int): The current line number for logging purposes.

        Returns:
            str or None: The value of the attribute if found, None otherwise.
        """
        value = None
        if attribute_name in attributes:
            if len(attributes[attribute_name]) != 1:
                logger.warning(
                    "Line %s: %s not set for this row's GeneAnnotation object due to more than one %s provided.",
                    curr_line_num,
                    attribute_name,
                    attribute_name,
                )
            elif attribute_name == "description":
                value = re.sub(
                    r"\s*\[Source.*?\]",
                    "",
                    urllib.parse.unquote(attributes["description"].pop()),
                )
            else:
                value = attributes[attribute_name].pop()
                if value.find(",") != -1:
                    logger.warning(
                        'Line %s: %s not set for this row\'s GeneAnnotation object due to value of %s attribute containing ",".',
                        curr_line_num,
                        attribute_name,
                        attribute_name,
                    )
                    value = None
        else:
            logger.warning(
                "Line %s: %s not set for this row's GeneAnnotation object due to missing %s attribute.",
                curr_line_num,
                attribute_name,
                attribute_name,
            )
        return value

    def __resolve_ncbi_gene_annotation(self, new_gene_annotation, curr_line_num):
        """
        Resolves conflicts between existing and new gene annotations based on certain conditions.

        Args:
            new_gene_annotation (GeneAnnotation): The new gene annotation to be resolved.
            curr_line_num (int): The current line number in the file.

        Returns:
            GeneAnnotation or None: The resolved gene annotation or None if it cannot be resolved
                                    or None if the resolution is in favor of the existing gene
                                    annotation.

        Raises:
            ValueError: If duplicates cannot be resolved.

        """
        existing_gene_annotation = self.gene_annotations[new_gene_annotation.id]
        if (
            existing_gene_annotation.description is None
            and new_gene_annotation.description is not None
        ):
            return new_gene_annotation
        if (
            existing_gene_annotation.description is not None
            and new_gene_annotation.description is None
        ):
            return None
        if (
            existing_gene_annotation.molecular_type is None
            and new_gene_annotation.molecular_type is not None
        ):
            return new_gene_annotation
        if (
            existing_gene_annotation.molecular_type is not None
            and new_gene_annotation.molecular_type is None
        ):
            return None
        if (
            existing_gene_annotation.molecular_type == ga.BioType.noncoding.value
            and new_gene_annotation.molecular_type != ga.BioType.noncoding.value
        ):
            return new_gene_annotation
        if (
            existing_gene_annotation.molecular_type != ga.BioType.noncoding.value
            and new_gene_annotation.molecular_type == ga.BioType.noncoding.value
        ):
            return None
        logger.critical(
            "Line %s: Unable to resolve duplicates for GeneID: %s.\nexisting gene: %s\nnew gene:      %s",
            curr_line_num,
            new_gene_annotation.id,
            existing_gene_annotation,
            new_gene_annotation,
        )
        return None

    def __merge_values(self, t):
        """
        Merge values from a list of lists into a dictionary of sets.

        Args:
            t (list): A list of lists containing key-value pairs.

        Returns:
            dict: A dictionary where each key maps to a set of values.

        """
        self.logger.debug("Merging values")
        result = defaultdict(set)
        for lst in t:
            key = lst[0].strip()
            value = lst[1:]
            for e in value:
                result[key].add(e.strip())
        return result

    def serialize_to_jsonld(
        self, exclude_none: bool = True, exclude_unset: bool = False
    ):
        """
        Serialize the object and either write it to the specified output file or print it to the CLI.

        Parameters:
            exclude_none (bool): Whether to exclude None values in the output.
            exclude_unset (bool): Whether to exclude unset values in the output.

        Returns:
            None
        """
        logger.debug("Serializing to JSON-LD")
        
        data = [
            self.organism_taxon.dict(
                exclude_none=exclude_none, exclude_unset=exclude_unset
            ),
            self.genome_assembly.dict(
                exclude_none=exclude_none, exclude_unset=exclude_unset
            ),
            self.genome_annotation.dict(
                exclude_none=exclude_none, exclude_unset=exclude_unset
            ),
        ]
        for ck in self.checksums:
            data.append(
                ck.dict(exclude_none=exclude_none, exclude_unset=exclude_unset)
            )
        for ga in self.gene_annotations.values():
            data.append(
                ga.dict(exclude_none=exclude_none, exclude_unset=exclude_unset)
            )

        output_data = {
            "@context": "https://raw.githubusercontent.com/brain-bican/models/main/jsonld-context-autogen/genome_annotation.context.jsonld",
            "@graph": data,
        }

        print(json.dumps(output_data, indent=2))

@click.command()
##ARGUEMENTS##
# Argument #1: The URL of the GFF file
@click.argument("content_url", type=str)

##OPTIONS##
# Option #1: The ID of the genome assembly
@click.option("assembly_accession", "-a", required=False, default = None, type=str)
# Option #2: The strain of the genome assembly
@click.option("--assembly_strain", "-s", required=False, default=None, type=str, help="The strain of the genome assembly. Defaults to None.")
# Option #3: A list of hash functions to use for generating checksums
@click.option("--hash_function", "-h", required=False, multiple=True, type=str, default=DEFAULT_HASH, help="A list of hash functions to use for generating checksums. Defaults to ('SHA256', 'MD5').")

def cli(content_url, assembly_accession, assembly_strain, hash_function, **args):
    hash_list = list(set(hash_function))
    gff3 = Gff3(content_url, assembly_accession, assembly_strain, hash_list)
    gff3.parse()
    gff3.serialize_to_jsonld()

if __name__ == "__main__":
    cli()

