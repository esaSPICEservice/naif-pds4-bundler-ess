"""Product Class and Child Classes Implementation."""
import datetime
import difflib
import filecmp
import glob
import logging
import os
import re
import shutil
import subprocess
from collections import OrderedDict
from datetime import date

import spiceypy

from ..utils import add_carriage_return
from ..utils import add_crs_to_file
from ..utils import checksum_from_label
from ..utils import checksum_from_registry
from ..utils import ck_coverage
from ..utils import compare_files
from ..utils import creation_time
from ..utils import current_date
from ..utils import dsk_coverage
from ..utils import extension2type
from ..utils import get_latest_kernel
from ..utils import match_patterns
from ..utils import md5
from ..utils import mk2list
from ..utils import pck_coverage
from ..utils import safe_make_directory
from ..utils import spk_coverage
from ..utils import type2extension
from ..utils import utf8len
from .label import BundlePDS4Label
from .label import ChecksumPDS4Label
from .label import DocumentPDS4Label
from .label import InventoryPDS4Label
from .label import MetaKernelPDS4Label
from .label import OrbnumFilePDS4Label
from .label import SpiceKernelPDS4Label
from .log import error_message
from .object import Object


class Product(object):
    """Parent Class that defines a generic archive product (or file)."""

    def __init__(self):
        """Constructor.

        Assigns values to the common attributes for all Products: file size,
        creation time and date, and file extension.
        """
        stat_info = os.stat(self.path)
        self.size = str(stat_info.st_size)

        #
        # Try to obtain the checksum from a checksum registry file, if not
        # present, try to obtain it from the label if the product is in the
        # staging area. Otherwise compute the checksum.
        #
        checksum = checksum_from_registry(self.path, self.setup.working_directory)

        if not checksum:
            checksum = checksum_from_label(self.path)
        if not checksum:
            checksum = str(md5(self.path))

        self.checksum = checksum
        self.setup.add_checksum(self.path, checksum)

        if hasattr(self.setup, "creation_date_time"):
            self.creation_time = self.setup.creation_date_time
        else:
            self.creation_time = creation_time(format=self.setup.date_format)

        self.creation_date = self.creation_time.split("T")[0]
        self.extension = self.path.split(os.sep)[-1].split(".")[-1]

        if self.new_product:
            self.setup.add_file(
                self.path.split(f"{self.setup.mission_acronym}_spice/")[-1]
            )

    def get_observer_and_target(self):
        """Read the configuration to extract the observers and the targets.

        :return: observers and targets
        :rtype: tuple
        """
        observers = []
        targets = []

        for pattern in self.collection.list.json_config.values():

            #
            # If the pattern is matched for the kernel name, extract
            # the target and observer from the kernel list
            # configuration.
            #
            if re.match(pattern["@pattern"], self.name):

                ker_config = self.setup.kernel_list_config[pattern["@pattern"]]

                #
                # Check if the kernel has specified targets.
                # Note that the mission target will not be used in this case.
                #
                if "targets" in ker_config:
                    targets = ker_config["targets"]["target"]
                #
                # If the kernel has no targets then the mission target is used.
                #
                else:
                    targets = [self.setup.target]

                #
                # Check if the kernel has specified observers.
                # Note that the mission observer will not be used in this case.
                #
                if "observers" in ker_config:
                    observers = ker_config["observers"]["observer"]
                #
                # If the kernel has no observers then the mission observer is
                # used.
                #
                else:
                    observers = [self.setup.observer]

                break
            #
            # If the product is not in the kernel list (such as the orbnum
            # file), then use the mission observers and targets.
            #
            else:
                observers = [self.setup.observer]
                targets = [self.setup.target]

        return (observers, targets)


class SpiceKernelProduct(Product):
    """Product child Class that defines a SPICE Kernel file archive product.

    :param setup: NPB run Setup Object
    :type setup: Object
    :param name: SPICE Kernel filename
    :type name: str
    :param collection: SPICE Kernel collection that will be a container of
                       the SPICE Kernel Product Object
    :type collection: Object
    """

    def __init__(self, setup, name, collection):
        """Constructor."""
        self.collection = collection
        self.setup = setup
        self.name = name

        self.extension = name.split(".")[-1].strip()
        self.type = extension2type(self)

        #
        # Determine if it is a binary or a text kernel.
        #
        if self.extension[0].lower() == "b":
            self.file_format = "Binary"
        else:
            self.file_format = "Character"

        if self.setup.pds_version == "4":
            self.lid = self.__product_lid()
            self.vid = self.__product_vid()
            ker_dir = "spice_kernels"
        else:
            ker_dir = "data"

        self.collection_path = setup.staging_directory + os.sep + ker_dir + os.sep

        product_path = self.collection_path + self.type + os.sep

        #
        # We generate the kernel directory if not present
        #
        safe_make_directory(product_path)

        #
        # We copy the kernel to the staging directory.
        #
        logging.info(f"-- Copy {self.name} to staging directory.")
        if not os.path.isfile(product_path + self.name):
            for path in self.setup.kernels_directory:
                try:
                    file = [
                        os.path.join(root, ker)
                        for root, dirs, files in os.walk(path)
                        for ker in files
                        if name == ker
                    ]

                    origin_path = file[0]

                    shutil.copy2(origin_path, product_path + os.sep + self.name)
                    self.new_product = True
                except BaseException:
                    try:
                        file = [
                            os.path.join(root, ker)
                            for root, dirs, files in os.walk(path)
                            for ker in files
                            if self.__product_mapping() == ker
                        ]

                        origin_path = file[0]

                        shutil.copy2(origin_path, product_path + os.sep + self.name)
                        self.new_product = True
                        logging.info(
                            f"-- Mapping {self.__product_mapping()} "
                            f"with {self.name}"
                        )
                    except BaseException:
                        error_message(f"{self.name} not present in {path}", setup=setup)

                self.check_kernel_integrity(self, origin_path)

                if self.new_product:
                    break
        else:
            logging.warning(f"     {self.name} already present in staging directory.")

            #
            # Even though it is not a 'new' product, the file is present in
            # the staging area because it was generated in a prior run.
            #
            self.new_product = True

        #
        # We update the path after having copied the kernel.
        #
        self.path = product_path + self.name

        self.__coverage()
        self.description = self.__read_description()

        Product.__init__(self)

        #
        # Extract the required information from the kernel list read from
        # configuration for the product.
        #
        (observers, targets) = self.get_observer_and_target()

        self.targets = targets
        self.observers = observers

        #
        # The kernel is labeled.
        #
        if self.setup.pds_version == "4":
            logging.info(f"-- Labeling {self.name}...")
            self.label = SpiceKernelPDS4Label(setup, self)

    def __product_lid(self):
        """Determine product logical identifier (LID).

        :return: product LID
        :rtype: str
        """
        product_lid = "{}:spice_kernels:{}_{}".format(
            self.setup.logical_identifier, self.type, self.name
        )

        return product_lid

    def __product_vid(self):

        product_vid = "1.0"

        return product_vid

    def __read_description(self):
        """Read the kernel list to return the description.

        The generated kernel list file must be used because it contains the
        description.

        :return:
        :rtype: tuple
        """
        kernel_list_file = (
            self.setup.working_directory
            + os.sep
            + f"{self.setup.mission_acronym}_{self.setup.run_type}_"
            f"{int(self.setup.release):02d}.kernel_list"
        )

        get_token = False
        description = False

        with open(kernel_list_file, 'r') as lst:
            for line in lst:
                if self.name in line:
                    get_token = True
                if get_token and "DESCRIPTION" in line:
                    description = line.split("=")[-1].strip()
                    get_token = False

        if not description:
            error_message(
                f"{self.name} does not have a description on " f"{kernel_list_file}",
                setup=self.setup,
            )

        return description

    def __coverage(self):

        #
        # Before computing the coverage we check if it the label is already
        # present.
        #
        coverage = []
        product_label = self.path.split(".")[0] + ".xml"
        if os.path.exists(product_label):
            with open(product_label, "r") as lbl:
                start = ""
                stop = ""
                for line in lbl:
                    if "<start_date_time>" in line:
                        start = line.split("<start_date_time>")[-1]
                        start = start.split("</start_date_time>")[0]
                    if "<stop_date_time>" in line:
                        stop = line.split("<stop_date_time>")[-1]
                        stop = stop.split("</stop_date_time>")[0]
                    if start and stop:
                        coverage = [start, stop]
                        logging.warning(
                            f"-- Coverage obtained from existing "
                            f"label: "
                            f"{product_label.split(os.sep)[-1]}"
                        )
                        break

        if not coverage:
            if self.type.lower() == "spk":
                coverage += spk_coverage(
                    self.path,
                    main_name=self.setup.spice_name,
                    date_format=self.setup.date_format,
                )
            elif self.type.lower() == "ck":
                coverage += ck_coverage(self.path, date_format=self.setup.date_format)
            elif self.extension.lower() == "bpc":
                coverage += pck_coverage(self.path, date_format=self.setup.date_format)
            elif self.type.lower() == "dsk":
                coverage += dsk_coverage(self.path, date_format=self.setup.date_format)
            else:
                coverage = [self.setup.mission_start, self.setup.mission_finish]

        self.start_time = coverage[0]
        self.stop_time = coverage[-1]

    def __ik_kernel_ids(self):
        """Extract the IDs from IK."""
        with open(f"{self.path}/{self.name}", "r") as f:

            id_list = list()
            parse_bool = False

            for line in f:

                if "begindata" in line:
                    parse_bool = True

                if "begintext" in line:
                    parse_bool = False

                if "INS-" in line and parse_bool:
                    line = line.lstrip()
                    line = line.split("_")
                    id_list.append(line[0][3:])

        id_list = list(set(id_list))

        return [id_list]

    def kernel_setup_phase(self):
        """Extract mission phase for kernel."""
        #
        # avoid reading the Z at the end of UTC tag
        #
        start_time = self.start_time[0:-1] if self.start_time != "N/A" else "N/A"
        stop_time = self.stop_time[0:-1] if self.stop_time != "N/A" else "N/A"

        setup_phase_map = (
            f"{self.setup.root_dir}/config/{self.setup.acronym.lower()}.phases"
        )

        with open(setup_phase_map, "r") as f:
            setup_phases = list()

            if start_time == "N/A" and stop_time == "N/A":

                for line in f:
                    setup_phases.append(line.split("   ")[0])

            if start_time != "N/A" and stop_time == "N/A":

                next_phases_bool = False
                start_tdb = spiceypy.str2et(start_time)  # + ' TDB')

                for line in f:
                    start_phase_date = line.rstrip().split(" ")[-2]
                    start_phase_tdb = spiceypy.str2et(start_phase_date)

                    stop_phase_date = line.rstrip().split(" ")[-1]
                    stop_phase_tdb = spiceypy.str2et(stop_phase_date)

                    if next_phases_bool:
                        setup_phases.append(line.split("   ")[0])

                    elif start_tdb >= start_phase_tdb:
                        setup_phases.append(line.split("   ")[0])
                        next_phases_bool = True

            if start_time != "N/A" and stop_time != "N/A":

                next_phases_bool = False
                start_tdb = spiceypy.str2et(start_time)  # + ' TDB')
                stop_tdb = spiceypy.str2et(stop_time)  # + ' TDB')

                for line in f:

                    start_phase_date = line.rstrip().split(" ")[-2]
                    start_phase_tdb = spiceypy.str2et(start_phase_date)  # + ' TDB')

                    stop_phase_date = line.rstrip().split(" ")[-1]
                    stop_phase_tdb = spiceypy.str2et(stop_phase_date)
                    stop_phase_tdb += 86400
                    #  One day added to account for gap between phases.

                    if next_phases_bool and start_phase_tdb >= stop_tdb:
                        break
                    # if next_phases_bool == True:
                    #     setup_phases.append(line.split('   ')[0])
                    # if start_phase_tdb <= start_tdb <= stop_phase_tdb:
                    #     setup_phases.append(line.split('   ')[0])
                    #     next_phases_bool = True
                    if start_tdb <= stop_phase_tdb:
                        setup_phases.append(line.split("   ")[0])
                        next_phases_bool = True

        setup_phases_str = ""
        for phase in setup_phases:
            setup_phases_str += "                                 " "" + phase + ",\r\n"

        setup_phases_str = setup_phases_str[0:-3]

        self.setup_phases = setup_phases_str

        return

    def __product_mapping(self):
        """Obtain the kernel mapping."""
        kernel_list_file = (
            self.setup.working_directory
            + os.sep
            + f"{self.setup.mission_acronym}_{self.setup.run_type}_"
            f"{int(self.setup.release):02d}.kernel_list"
        )

        get_map = False
        mapping = False

        with open(kernel_list_file, 'r') as lst:
            for line in lst:
                if self.name in line:
                    get_map = True
                if get_map and "MAPPING" in line:
                    mapping = line.split("=")[-1].strip()
                    get_map = False

        if not mapping:
            error_message(
                f"{self.name} does not have mapping on {kernel_list_file}",
                setup=self.setup,
            )

        return mapping

    def check_kernel_integrity(self, object, path):
        """Check if the kernels have the adequate architecture."""
        #
        # All files that are to have labels generated must have a NAIF
        # file ID word as the first "word" on the first line of the
        # file. Check if it is the case.
        #
        (arch, type) = spiceypy.getfat(path)

        if object.file_format == "Binary":

            #
            # A binary file could have the following architectures:
            #
            #   DAF - The file is based on the DAF architecture.
            #   DAS - The file is based on the DAS architecture.
            #   XFR - The file is in a SPICE transfer file format.
            #
            # But for an archive only DAF is acceptable.
            #
            if (arch != "DAF") and (arch != "DAS"):
                error_message(
                    f"Kernel {object.name} architecture {arch} is invalid",
                    setup=object.setup,
                )
            else:
                pass
        elif object.file_format == "Character":

            #
            # Text kernels must have KPL architecture:
            #
            #    KPL -- Kernel Pool File (i.e., a text kernel)
            #
            if arch != "KPL":
                error_message(
                    f"Kernel {object.name} architecture {arch} is invalid",
                    setup=object.setup,
                )

        if type != object.type.upper():
            error_message(
                f"Kernel {object.name} type {object.type.upper()} "
                f"is not the one expected: {type}",
                setup=object.setup,
            )

        return


class MetaKernelProduct(Product):
    """Product child class Meta-Kernel."""

    def __init__(self, setup, kernel, spice_kernels_collection, user_input=False):
        """Constructor.

        :param mission:
        :param spice_kernels_collection:
        :param product: We can input a meta-kernel such that the meta-kernel
                        does not have to be generated
        """
        if user_input:
            logging.info(f"-- Copy meta-kernel: {kernel}")
            self.path = kernel
        else:
            logging.info(f"-- Generate meta-kernel: {kernel}")
            self.template = f"{setup.root_dir}templates/template_metakernel.tm"
            self.path = self.template

        self.new_product = True
        self.setup = setup
        self.collection = spice_kernels_collection
        self.file_format = "Character"

        if os.sep in kernel:
            self.name = kernel.split(os.sep)[-1]
        else:
            self.name = kernel

        self.extension = self.path.split(".")[1]

        self.type = extension2type(self.name)

        #
        # Add the configuration items for the meta-kernel.
        # This includes sorting out the meta-kernel name.
        #
        if hasattr(setup, "mk"):
            for metak in setup.mk:

                patterns = []
                for name in metak["name"]:
                    name_pattern = name["pattern"]
                    if not isinstance(name_pattern, list):
                        patterns.append(name_pattern)
                    else:
                        patterns += name_pattern

                try:
                    values = match_patterns(self.name, metak["@name"], patterns)
                    self.mk_setup = metak
                    self.version = values["VERSION"]
                    self.values = values
                    #
                    # If it is a yearly meta-kernel we need the year to set
                    # set the coverage of the meta-kernel.
                    #
                    if "YEAR" in values:
                        self.year = values["YEAR"]
                        self.YEAR = values["YEAR"]
                except BaseException:
                    pass

            if not hasattr(self, "mk_setup"):
                error_message(
                    f"Meta-kernel {self.name} has not been matched "
                    f"in configuration",
                    setup=self.setup,
                )

        if setup.pds_version == "3":
            self.collection_path = setup.staging_directory + os.sep + "extras" + os.sep
        elif setup.pds_version == "4":
            self.collection_path = (
                setup.staging_directory + os.sep + "spice_kernels" + os.sep
            )

        if self.setup.pds_version == "3":
            product_path = self.collection_path + self.type + os.sep
            self.KERNELPATH = "./data"
        else:
            product_path = self.collection_path + self.type + os.sep
            self.KERNELPATH = ".."

        if self.setup.pds_version == "4":
            self.AUTHOR = self.setup.producer_name
        else:
            self.AUTHOR = self.setup.producer_name

        self.PDS4_MISSION_NAME = self.setup.mission_name

        if hasattr(self.setup, "creation_date_time"):
            self.MK_CREATION_DATE = current_date(date=self.setup.creation_date_time)
        else:
            self.MK_CREATION_DATE = current_date()
        self.SPICE_NAME = self.setup.spice_name
        self.INSTITUTION = self.setup.institution

        #
        # Generate the meta-kernel directory if not present
        #
        safe_make_directory(product_path)

        #
        # Name the meta-kernel; if the meta-kernel is manually provided this
        # step is skipped.
        #
        if user_input:
            self.name = kernel.split(os.sep)[-1]
            self.path = kernel

            #
            # If the meta-kernel is provided by the user check the
            # integrity.
            #
            SpiceKernelProduct.check_kernel_integrity(self, self, self.path)
        else:
            self.path = product_path + self.name

        self.FILE_NAME = self.name

        #
        # Check product version if the current archive is not the
        # first release or if the mk_setup configuration section has been
        # provided.
        #
        if self.setup.increment and hasattr(self.setup, "mk_setup"):
            self.check_version()

        #
        # Generate the product LIDVID.
        #
        if self.setup.pds_version == "4":
            self.set_product_lid()
            self.set_product_vid()

            #
            # The meta-kernel must be named before fetching the description.
            #
            self.description = self.get_description()

        #
        # Generate the meta-kernel.
        #
        if not user_input:
            if os.path.exists(self.path):
                logging.warning(f"-- Meta-kernel already exists: {self.path}")
                logging.warning(
                    "-- The meta-kernel will be generated and the one "
                    "present in the staging are will be overwritten."
                )
                logging.warning(
                    "-- Note that to provide a meta-kernel as an input, "
                    "it must be provided via configuration file."
                )
            self.write_product()
        else:
            # Implement manual provision of meta-kernel.
            shutil.copy2(self.path, f"{product_path}{self.name}")
            self.path = f"{product_path}{self.name}"

        self.new_product = True

        #
        # Following the product generation we read the kernels again to
        # include all the kernels present.
        #
        self.collection_metakernel = mk2list(self.path, self.setup)

        #
        # Set the meta-kernel times
        #
        self.coverage()

        #
        # Extract the required information from the kernel list read from
        # configuration for the product.
        #
        (observers, targets) = self.get_observer_and_target()

        self.targets = targets
        self.observers = observers

        Product.__init__(self)

        if self.setup.pds_version == "4":
            logging.info("")
            logging.info(f"-- Labeling meta-kernel: {self.name}...")
            self.label = MetaKernelPDS4Label(setup, self)

        return

    def check_version(self):
        """Check if the provided Meta-kernel version is correct."""
        #
        # Distinguish in between the different kernels we can find in the
        # previous increment.
        #
        pattern = self.mk_setup["@name"]
        for key in self.values:

            #
            # So far, only the pattern key YEAR is incorporated to sort out
            # the version of the meta-kernel name.
            #
            if key == "YEAR":
                pattern = pattern.replace("$" + key, self.year)
            else:
                pattern = pattern.replace("$" + key, "?" * len(self.values[key]))

        versions = glob.glob(
            f"{self.setup.bundle_directory}/"
            f"{self.setup.mission_acronym}_spice/"
            f"spice_kernels/mk/{pattern}"
        )

        versions.sort()
        try:
            version_index = pattern.find("?")

            version = versions[-1].split(os.sep)[-1]
            version = version[version_index : version_index + len(self.values[key])]
            version = int(version) + 1

        except BaseException:
            logging.warning("-- Meta-kernel from previous increment is not available.")
            logging.warning(f"   Version will be set to: {self.version}.")

            return

        if version == int(self.version):
            logging.info(
                f"-- Version from kernel list and from previous "
                f"increment agree: {version}."
            )
        else:
            logging.warning(
                "-- The meta-kernel version is not as expected "
                "from previous increment."
            )
            logging.warning(
                f"   Version set to: {int(self.version)}, whereas "
                f"it is expected to be: {version}."
            )
            logging.warning(
                "   It is recommended to stop the execution and fix the issue."
            )

        return

    def set_product_lid(self):
        """Set the Meta-kernel LID."""
        if self.type == "mk":
            name = self.name.split("_v")[0]
        else:
            name = self.name

        product_lid = "{}:spice_kernels:{}_{}".format(
            self.setup.logical_identifier, self.type, name
        )

        self.lid = product_lid

    def set_product_vid(self):
        """Set the Meta-kernel VID.

        If the meta-kernel has been automatically generated as indicated in
        the configuration file, it has a version attribute, otherwise it does
        not and its version number must be equal to the expected VID.
        """
        try:
            product_vid = str(self.version).lstrip("0") + ".0"
        except BaseException:
            logging.warning(
                f"-- {self.name} No VID explicit in kernel name: set to 1.0"
            )
            logging.warning(
                "-- Make sure that the MK pattern in the "
                "configuration file is correct, if manually provided make sure "
                "you provided the appropriate name."
            )
            product_vid = "1.0"

            #
            # Implement this exceptional check for first releases of an archive.
            #
            if "01" not in self.name:
                error_message(
                    f"{self.name} version does not correspond to VID "
                    f"1.0. Rename the MK accordingly",
                    setup=self.setup,
                )

        self.vid = product_vid

    def get_description(self):
        """Obtain the kernel product description information."""
        description = ""
        self.json_config = self.setup.kernel_list_config

        kernel = self.name
        for pattern in self.setup.re_config:

            if pattern.match(kernel):

                description = self.json_config[pattern.pattern]["description"]
                try:
                    patterns = self.json_config[pattern.pattern]["patterns"]
                except BaseException:
                    patterns = False
                # try:
                #     options = self.json_config[pattern.pattern]["mklabel_options"]
                # except BaseException:
                #     options = ""
                # try:
                #     mapping = self.json_config[pattern.pattern]["mapping"]
                # except BaseException:
                #     mapping = ""

                #
                # ``options'' and ``descriptions'' require to
                # substitute parameters derived from the filenames
                # themselves.
                #
                if patterns:
                    for el in patterns:
                        if ("$" + el) in description:
                            value = patterns[el]

                            #
                            # There are two distinct patterns:
                            #    * extracted form the filename
                            #    * defined in the configuration file.
                            #
                            if (
                                "@pattern" in patterns[el]
                                and patterns[el]["@pattern"].lower() == "kernel"
                            ):
                                #
                                # When extracted from the filename, the
                                # keyword is matched in between patterns.
                                #

                                #
                                # First Turn the regex set into a single
                                # character to be able to know were in the
                                # filename is.
                                #
                                patt_ker = value["#text"].replace("[0-9]", "$")
                                patt_ker = patt_ker.replace("[a-z]", "$")
                                patt_ker = patt_ker.replace("[A-Z]", "$")
                                patt_ker = patt_ker.replace("[a-zA-Z]", "$")

                                #
                                # Split the resulting pattern to build up the
                                # indexes to extract the value from the kernel
                                # name.
                                #
                                patt_split = patt_ker.split(f"${el}")

                                #
                                # Create a list with the length of each part.
                                #
                                indexes = []
                                for element in patt_split:
                                    indexes.append(len(element))

                                #
                                # Extract the value with the index from the
                                # kernel name.
                                #
                                if len(indexes) == 2:
                                    value = kernel[
                                        indexes[0] : len(kernel) - indexes[1]
                                    ]
                                    if patterns[el]["@pattern"].isupper():
                                        value = value.upper()
                                        #
                                        # Write the value of the pattern for
                                        # future use.
                                        #
                                        patterns[el]["&value"] = value
                                else:
                                    error_message(
                                        f"Kernel pattern {patt_ker} "
                                        f"not adept to write "
                                        f"description. Remember a "
                                        f"metacharacter "
                                        f"cannot start or finish "
                                        f"a kernel pattern",
                                        setup=self.setup,
                                    )
                            else:
                                #
                                # For non-kernels the value is based on the
                                # value within the tag that needs to be
                                # provided by the user; there is no way this
                                # can be done automatically.
                                #
                                for val in patterns[el]:
                                    if kernel == val["@value"]:
                                        value = val["#text"]
                                        #
                                        # Write the value of the pattern for
                                        # future use.
                                        #
                                        patterns[el]["&value"] = value

                                if isinstance(value, list):
                                    error_message(
                                        f"-- Kernel description "
                                        f"could not be updated with "
                                        f"pattern: {value}",
                                        setup=self.setup,
                                    )

                            description = description.replace("$" + el, value)

        description = description.replace("\n", "")
        while "  " in description:
            description = description.replace("  ", " ")

        if not description:
            error_message(
                f"{self.name} does not have a description on configuration file",
                setup=self.setup,
            )

        return description

    def write_product(self):
        """Write the Meta-kernel."""
        #
        # Obtain meta-kernel grammar from configuration.
        #
        kernel_grammar_list = self.mk_setup["grammar"]["pattern"]

        #
        # We scan the kernel directory to obtain the list of available kernels
        #
        kernel_type_list = ["lsk", "pck", "fk", "ik", "sclk", "spk", "ck", "dsk"]

        #
        # Setup the end-of-line character
        #
        eol = self.setup.eol_mk

        #
        # All the files of the directory are read into a list
        #
        mkgen_kernels = []
        excluded_kernels = []

        for kernel_type in kernel_type_list:

            #
            # First we build the list of excluded kernels
            #
            for kernel_grammar in kernel_grammar_list:
                if "exclude:" in kernel_grammar:
                    excluded_kernels.append(kernel_grammar.split("exclude:")[-1])

            logging.info(
                f"     Matching {kernel_type.upper()}(s) with meta-kernel grammar."
            )
            for kernel_grammar in kernel_grammar_list:

                if "date:" in kernel_grammar:
                    kernel_grammar = kernel_grammar.split("date:")[-1]
                    dates = True
                else:
                    dates = False

                #
                # Kernels can come from several kernel directories or from the
                # previous meta-kernel. Paths are provided by the parameter
                # paths and meta-kernels with mks.
                #
                paths = []
                mks = []
                if kernel_grammar.split(".")[-1].lower() in type2extension(kernel_type):
                    try:
                        if self.setup.pds_version == "3":
                            paths.append(self.setup.staging_directory + "/DATA")

                        else:
                            paths.append(
                                self.setup.staging_directory + "/spice_kernels"
                            )
                        # paths.append(self.setup.kernels_directory)

                        #
                        # Try to look for meta-kernels from previous
                        # increments.
                        #
                        try:
                            mks = glob.glob(
                                f"{self.setup.bundle_directory}/"
                                f"{self.setup.mission_acronym}"
                                f"_spice/spice_kernels/mk/"
                                f'{self.name.split("_v")[0]}*.tm'
                            )
                        except BaseException:
                            if self.setup.increment:
                                logging.warning(
                                    "-- No meta-kernels from "
                                    "previous increment "
                                    "available."
                                )

                        latest_kernel = get_latest_kernel(
                            kernel_type,
                            paths,
                            kernel_grammar,
                            dates=dates,
                            excluded_kernels=excluded_kernels,
                            mks=mks,
                        )
                    except Exception as e:
                        logging.warning(f"-- Exception: {e}")
                        latest_kernel = []

                    if latest_kernel:
                        if not isinstance(latest_kernel, list):
                            latest_kernel = [latest_kernel]

                        for kernel in latest_kernel:
                            logging.info(f"        Matched: {kernel}")
                            mkgen_kernels.append(kernel_type + "/" + kernel)

        #
        # Remove duplicate entries from the kernel list (possible depending on
        # the grammar).
        #
        mkgen_kernels = list(OrderedDict.fromkeys(mkgen_kernels))

        #
        # Subset the SPICE kernels collection with the kernels in the MK
        # only.
        #
        collection_metakernel = []
        for spice_kernel in self.collection.product:
            for name in mkgen_kernels:
                if spice_kernel.name in name:
                    collection_metakernel.append(spice_kernel)

        self.collection_metakernel = collection_metakernel

        #
        # Report kernels present in meta-kernel
        #
        logging.info("")
        logging.info("-- Archived kernels present in meta-kernel")
        for kernel in collection_metakernel:
            logging.info(f"     {kernel.name}")
        logging.info("")

        num_ker_total = len(self.collection.product)
        num_ker_mk = len(collection_metakernel)

        logging.warning(f"-- Archived kernels:           {num_ker_total}")
        logging.warning(f"-- Kernels in meta-kernel:     {num_ker_mk}")

        #
        # The kernel list for the new mk is formatted accordingly
        #
        kernels = ""
        kernel_dir_name = None
        for kernel in mkgen_kernels:

            if kernel_dir_name:
                if kernel_dir_name != kernel.split(".")[1]:
                    kernels += eol

            kernel_dir_name = kernel.split(".")[1]

            kernels += f"{' ' * 26}'$KERNELS/{kernel}'{eol}"

        self.KERNELS_IN_METAKERNEL = kernels

        #
        # Introduce and curate the rest of fields from configuration
        #
        if "data" in self.mk_setup["metadata"]:
            data = self.mk_setup["metadata"]["data"]
        else:
            data = ""
        if "description" in self.mk_setup["metadata"]:
            desc = self.mk_setup["metadata"]["description"]
        else:
            desc = ""

        curated_data = ""
        curated_desc = ""

        first_line = True
        for line in data.split("\n"):
            #
            # We want to remove the blanks if the line is empty.
            #
            if line.strip() == "":
                curated_data += ""
            else:
                if first_line:
                    curated_data += eol
                first_line = False
                curated_data += " " * 6 + line.strip() + eol

        metakernel_dictionary = vars(self)

        for line in desc.split("\n"):
            #
            # We want to remove the blanks if the line is empty, and replace
            # any possible keywords.
            #
            if line.strip() == "":
                curated_desc += eol
            else:
                for key, value in metakernel_dictionary.items():
                    if (
                        isinstance(value, str)
                        and key.isupper()
                        and key in line
                        and "$" in line
                    ):
                        line = line.replace("$" + key, value)
                curated_desc += " " * 3 + line.strip() + eol

        self.DATA = curated_data
        self.DESCRIPTION = curated_desc

        metakernel_dictionary = vars(self)

        with open(self.path, "w+") as f:
            with open(self.template, "r") as t:
                for line in t:
                    line = line.rstrip()
                    for key, value in metakernel_dictionary.items():
                        if (
                            isinstance(value, str)
                            and key.isupper()
                            and key in line
                            and "$" in line
                        ):
                            line = line.replace("$" + key, value)
                    f.write(line + eol)

        self.product = self.path

        logging.info("-- Meta-kernel generated.")
        if not self.setup.args.silent and not self.setup.args.verbose:
            print(
                "   * Created "
                f"{self.product.split(self.setup.staging_directory)[-1]}."
            )

        #
        # If the meta-kernel has been generated by NPB with a given grammar,
        # etc. pause the pipeline to allow the user to introduce changes.
        #
        if hasattr(self, "mk_setup") and not self.setup.args.debug:
            print("    * The meta-kernel might need to be updated. You can:")
            print(
                '        - Type "vi" and press ENTER to edit the '
                "file with the VI text editor."
            )
            print(
                "        - Edit the file with your favorite edit "
                "and press ENTER and continue."
            )
            print("        - Press ENTER to continue.")
            print(f"      MK path: {self.path}")

            inp = input(">> Type 'vi' and/or press ENTER to continue... ")
            if str(inp).lower() == "vi":
                try:
                    cmd = os.environ.get("EDITOR", "vi") + " " + self.path
                    subprocess.call(cmd, shell=True)
                    logging.warning("Meta-kernel edited with Vi by the " "user.")
                except BaseException:
                    print("Vi text editor is not available.")
                    input(">> Press Enter to continue... ")

        if self.setup.diff:
            self.compare()
        logging.info("")

        return

    def compare(self):
        """Compare the Meta-kernel with the previous version."""
        val_mk = ""

        #
        # Compare meta-kernel with latest. First try with previous increment.
        #
        try:
            match_flag = True
            val_mk_path = (
                f"{self.setup.bundle_directory}/"
                f"{self.setup.mission_acronym}_spice/spice_kernels/mk/"
            )

            val_mk_name = self.name.split(os.sep)[-1]
            i = 1

            while match_flag:
                if i < len(val_mk_name) - 1:
                    val_mks = glob.glob(val_mk_path + val_mk_name[0:i] + "*.tm")
                    if val_mks:
                        val_mks = sorted(val_mks)
                        val_mk = val_mks[-1]
                        match_flag = True
                    else:
                        match_flag = False
                    i += 1

            if not val_mk:
                raise Exception("No label for comparison found.")

        except BaseException:
            #
            # If previous increment does not work, compare with insight
            # example.
            #
            logging.warning(f"-- No other version of {self.name} has been found.")
            logging.warning("-- Comparing with meta-kernel template.")

            val_mk = f"{self.setup.root_dir}/templates/template_metakernel.tm"

        fromfile = self.path
        tofile = val_mk
        dir = self.setup.working_directory

        logging.info(
            f"-- Comparing "
            f'{self.name.split(f"{self.setup.mission_acronym}_spice/")[-1]}'
            f"..."
        )
        compare_files(fromfile, tofile, dir, self.setup.diff)

    def validate(self):
        """Perform a basic validation of the Meta-kernel.

        The validation consists of:

           * load the kernel with ``FURNSH``
           * Count the loaded kernels with ``KTOTAL``
           * Compare the number of kernel sin the pool with the length of the
             meta-kernel collection list attribute.
        """
        line = f"Step {self.setup.step} - Meta-kernel {self.name} validation"
        logging.info("")
        logging.info(line)
        logging.info("-" * len(line))
        logging.info("")
        self.setup.step += 1
        if not self.setup.args.silent and not self.setup.args.verbose:
            print("-- " + line.split(" - ")[-1] + ".")

        rel_path = self.path.split(f"/{self.setup.mission_acronym}_spice/")[-1]
        path = (
            self.setup.bundle_directory.split(f"{self.setup.mission_acronym}_spice")[0]
            + f"/{self.setup.mission_acronym}_spice/"
            + rel_path
        )

        cwd = os.getcwd()
        mkdir = os.sep.join(path.split(os.sep)[:-1])
        os.chdir(mkdir)

        spiceypy.kclear()
        try:
            spiceypy.furnsh(path)
        except Exception:
            error_message("SpiceyPy error", setup=self.setup)

        #
        # In KTOTAL, all meta-kernels are counted in the total; therefore
        # we need to substract 1 kernel.
        #
        ker_num_fr = spiceypy.ktotal("ALL") - 1
        ker_num_mk = self.collection_metakernel.__len__()

        logging.info(f"-- Kernels loaded with FURNSH: {ker_num_fr}")
        logging.info(f"-- Kernels present in {self.name}: {ker_num_mk}")

        if ker_num_fr != ker_num_mk:
            spiceypy.kclear()
            error_message(
                "Number of kernels loaded is not equal to kernels "
                "present in meta-kernel",
                setup=self.setup,
            )

        spiceypy.kclear()

        os.chdir(cwd)

        return

    def coverage(self):
        """Determine Meta-kernel coverage.

        Meta-kernel coverage is determined by:

        *  for whole mission meta-kernels start_date_time and stop_date_time
           are set to the coverage provided by spacecraft SPK or CKs, at the
           discretion of the archive producer.

        *  for yearly mission meta-kernels start_date_time and stop_date_time
           are set to the coverage from Jan 1 00:00 of the year to either the
           end of coverage provided by spacecraft SPK or CKs, or the end of
           the year (whichever is earlier)

        :return:
        """
        kernels = []

        #
        # Match the pattern with the kernels in the meta-kernel.
        # Only the kernel patterns identified with the meta-kernel attribute
        # will be used.
        #
        if hasattr(self.setup, "coverage_kernels"):
            coverage_kernels = self.setup.coverage_kernels
            patterns = coverage_kernels[0]["pattern"]
            if not isinstance(patterns, list):
                patterns = [patterns]

            #
            # It is assumed that the coverage kernel is present in the
            # meta-kernel.
            #
            for pattern in patterns:
                #
                # Only match coverage kernels that have the MK pattern in the
                # mk attribute.
                #
                if re.match(pattern["@mk"], self.name):
                    for kernel in self.collection_metakernel:
                        if re.match(pattern["#text"], kernel):
                            kernels.append(kernel)
                            self.mk_sets_coverage = True

        start_times = []
        finish_times = []
        if kernels:
            #
            # Look for the identified kernel in the collection, if the kernel
            # is not present the coverage will have to be computed.
            #
            if kernels:
                for kernel in kernels:
                    ker_found = False
                    for product in self.collection.product:
                        if kernel == product.name:
                            start_times.append(spiceypy.utc2et(product.start_time[:-1]))
                            finish_times.append(spiceypy.utc2et(product.stop_time[:-1]))
                            ker_found = True

                    #
                    # When the kernels are not present in the current
                    # collection, the coverage is computed.
                    #
                    if not ker_found:
                        path = (
                            f"{self.setup.bundle_directory}/"
                            f"{self.setup.mission_acronym}_spice/"
                            f"spice_kernels/"
                            f"{extension2type(kernel)}/{kernel}"
                        )

                        #
                        # Added check of file size for test cases.
                        #
                        if not os.path.exists(path) or os.path.getsize(path) == 0:
                            logging.warning(
                                f"-- File not present in final area: {path}."
                            )
                            logging.warning(
                                "   It will not be used to determine the coverage."
                            )
                        else:
                            if extension2type(kernel) == "spk":
                                (start_time, stop_time) = spk_coverage(
                                    path, main_name=self.setup.spice_name
                                )
                            elif extension2type(kernel) == "ck":
                                (start_time, stop_time) = ck_coverage(path)
                            else:
                                error_message(
                                    "Kernel used to determine "
                                    "coverage is not a SPK or CK "
                                    "kernel",
                                    setup=self.setup,
                                )

                            start_times.append(spiceypy.utc2et(start_time[:-1]))
                            finish_times.append(spiceypy.utc2et(stop_time[:-1]))

                            logging.info(
                                f"-- File {kernel} used to determine coverage."
                            )

        #
        # If it is a yearly meta-kernel; we need to handle it separately.
        #
        if hasattr(self, "year") and start_times and finish_times:

            #
            # The date can be January 1st, of the year or the mission start
            # date, but it should not be later or earlier than that.
            #
            et_mission_start = spiceypy.utc2et(self.setup.mission_start[:-1])
            et_year_start = spiceypy.utc2et(f"{self.year}-01-01T00:00:00")

            if et_year_start > et_mission_start:
                start_times = [et_year_start]
            else:
                start_times = [et_mission_start]

            #
            # Update the end time of the meta-kernel
            #
            et_year_stop = spiceypy.utc2et(f"{int(self.year) + 1}-01-01T00:00:00")

            if max(finish_times) > et_year_stop:
                finish_times = [et_year_stop]

        try:

            start_time = spiceypy.et2utc(min(start_times), "ISOC", 0, 80) + "Z"
            stop_time = spiceypy.et2utc(max(finish_times), "ISOC", 0, 80) + "Z"
            logging.info(f"-- Meta-kernel coverage: {start_time} - {stop_time}")

        except BaseException:
            #
            # The alternative is to set the increment stop time to the
            # end time of the mission.
            #
            start_time = self.setup.mission_start
            stop_time = self.setup.mission_finish
            logging.warning(
                "-- No kernel(s) found to determine meta-kernel "
                "coverage. Mission times will be used:"
            )
            logging.warning(f"   {start_time} - {stop_time}")

        self.start_time = start_time
        self.stop_time = stop_time

        return


class OrbnumFileProduct(Product):
    """Product child class ORBNUM File."""

    def __init__(self, setup, name, collection, kernels_collection):
        """Constructor."""
        self.collection = collection
        self.kernels_collection = kernels_collection
        self.setup = setup
        self.name = name
        self.extension = name.split(".")[-1].strip()
        self.path = setup.orbnum_directory
        self.collection_path = setup.staging_directory + os.sep + "miscellaneous"
        product_path = self.collection_path + os.sep + "orbnum" + os.sep

        #
        # Map the orbnum file with its configuration.
        #
        for orbnum_type in setup.orbnum:
            if re.match(orbnum_type["pattern"], name):
                self._orbnum_type = orbnum_type
                self._pattern = orbnum_type["pattern"]

        if not hasattr(self, "_orbnum_type"):
            error_message(
                "The orbnum file does not match any type "
                "described in the configuration",
                setup=self.setup,
            )

        self.__set_product_lid()
        self.__set_product_vid()

        #
        # We generate the kernel directory if not present
        #
        safe_make_directory(product_path)

        #
        # We copy the file to the staging directory.
        #
        logging.info(f"-- Copy {self.name} to staging directory.")
        if not os.path.isfile(product_path + self.name):
            shutil.copy2(
                self.path + os.sep + self.name, product_path + os.sep + self.name
            )
            self.new_product = True
        else:
            logging.warning(f"     {self.name} already present in staging directory.")

            self.new_product = False

        #
        # Add CRs to the orbnum file
        #
        add_crs_to_file(product_path + os.sep + self.name, self.setup.eol, self.setup)

        #
        # We update the path after having copied the kernel.
        #
        self.path = product_path + self.name

        #
        # We obtain the parameters required to fill the label.
        #
        header = self.__read_header()
        self.__set_event_detection_key(header)
        self.header_length = self.__get_header_length()
        self.__set_previous_orbnum()
        self.__read_records()
        self._sample_record = self.__get_sample_record()
        self.description = self.__get_description()

        #
        # Table character description is only obtained if there are missing
        # records.
        #
        self.table_char_description = self.__table_character_description()

        self.__get_params(header)
        self.__set_params(header)

        self.__coverage()

        #
        # Extract the required information from the kernel list read from
        # configuration for the product.
        #
        (observers, targets) = self.get_observer_and_target()

        self.targets = targets
        self.observers = observers

        Product.__init__(self)

        #
        # The kernel is labeled.
        #
        logging.info(f"-- Labeling {self.name}...")
        self.label = OrbnumFilePDS4Label(setup, self)

        return

    def __set_product_lid(self):
        """Set the Product LID."""
        self.lid = "{}:miscellaneous:orbnum_{}".format(
            self.setup.logical_identifier, self.name
        )

    def __set_product_vid(self):
        """Set the Product VID."""
        self.vid = "1.0"

    def __set_previous_orbnum(self):
        """Determine the previou version of the ORBNUM file.

        For some cases, more than one orbit number file
        may exist for a given SPK, with only one file having the same name
        as the SPK and other files having a version token appended to the
        SPK name. It is also possible that a version token is always present.
        This method finds the latest version of such an orbnum file in the
        final area.

        NPB assumes that the version pattern of the orbnum file name follows
        the REGEX pattern::

           ``_[vV][0-9]*[.]``

        E.g.: ``(...)_v01.orb``   or
              ``(...)_V9999.nrb`` or
              ``(...)_v1.orb``

        This method provides values to the ``_previous_orbnum`` and
        ``_orbnum_version`` protected attributes, both are strings.
        """
        path = [
            f"{self.setup.bundle_directory}/{self.setup.mission_acronym}"
            f"_spice//miscellaneous/"
        ]
        previous_orbnum = get_latest_kernel("orbnum", path, self._pattern, dates=True)

        if previous_orbnum:

            version_pattern = r"_[vV][0-9]*[\.]"
            version_match = re.search(version_pattern, previous_orbnum[0])
            version = re.findall(r"\d+", version_match.group(0))
            version = "".join(version)

            self._previous_version = version
            self.previous_orbnum = previous_orbnum[0]

        #
        # If a previous orbnum file is not found, it might be due to the
        # fact that the file does not have a version explicitely indicated
        # by the filename.
        #
        else:

            #
            # Try to remove the version part of the filename pattern. For
            # the time being this implementation is limited to NAIF
            # orbnum files with this characteristics.
            #
            try:
                version_pattern = r"_[vV]\[0\-9\]*[\.]"
                version_match = re.search(version_pattern, self._pattern)
                pattern = ".".join(self._pattern.split(version_match.group(0)))
            except BaseException:
                #
                # The pattern already does not have an explicit version
                # number.
                #
                pattern = self._pattern

            previous_orbnum = get_latest_kernel("orbnum", path, pattern, dates=True)
            if not previous_orbnum:
                self._previous_orbnum = ""
            else:
                self._previous_orbnum = previous_orbnum

            self._previous_version = "1"

    def __read_header(self):
        """Read and process an orbnum file header.

        Defines the record_fixed_length attribute that provides the lenght of
        a record.

        :return: header line
        :rtype: str
        """
        header = []
        with open(self.path, "r") as o:

            if int(self._orbnum_type["header_start_line"]) > 1:
                for _i in range(int(self._orbnum_type["header_start_line"]) - 1):
                    o.readline()

            header.append(o.readline())
            header.append(o.readline())

        #
        # Perform a minimal check to determine if the orbnum file has
        # the appropriate header. Include the old ORBNUM utility header format
        # for Descending and Ascending node events (Odyssey).
        #
        if ("Event" not in header[0]) and ("Node" not in header[0]):
            error_message(
                f"The header of the orbnum file {self.name} is not as expected",
                setup=self.setup,
            )

        if "===" not in header[1]:
            error_message(
                f"The header of the orbnum file {self.name} is not as expected",
                setup=self.setup,
            )

        #
        # Set the fixed record length from the header.
        #
        self.record_fixed_length = len(header[1])

        return header

    def __get_header_length(self):
        """Read an orbnum file and return the length of the header in bytes.

        :return: header line
        :rtype: str
        """
        header_length = 0
        with open(self.path, "r") as o:
            lines = 0
            header_start = int(self._orbnum_type["header_start_line"])
            for line in o:
                lines += 1
                if lines < header_start + 2:
                    header_length += utf8len(line)
                else:
                    break

        return header_length + 1

    def __get_sample_record(self):
        """Read an orbnum file and return one record sample.

        This sample (one data line) will be used to determine the format of
        each parameter of the orbnum file. The sample is re-processed in such a
        way that it contains no spaces for the UTC dates.

        :return: sample record line
        :rtype: str
        """
        sample_record = ""
        with open(self.path, "r") as o:
            lines = 0
            header_start = int(self._orbnum_type["header_start_line"])
            for line in o:
                lines += 1
                if lines > header_start + 1:
                    #
                    # The original condition was: `if line.strip():`
                    # But some orbnum files have records that only have the
                    # orbit number and nothing else. E.g.:
                    #
                    # header[0]        No.     Event UTC PERI
                    # header[1]      =====  ====================
                    # skip               2
                    # sample_record      3  1976 JUN 22 18:07:09
                    #
                    if len(line.strip()) > 5:
                        sample_record = line
                        break

        if not sample_record:
            error_message("The orbnum file has no records", setup=self.setup)

        sample_record = self.__utc_blanks_to_dashes(sample_record)

        return sample_record

    def __utc_blanks_to_dashes(self, sample_record):
        """Reformat UTC strings in the ORBNUM file.

        Re-process the UTC string fields of an orbnum sample row
        to remove blankspaces.

        :param sample_record: sample row with UTC string with blankspaces
        :type sample_record: str
        :return: sample row with UTC string with dashes
        :rtype: str
        """
        utc_pattern = r"[0-9]{4}[ ][A-Z]{3}[ ][0-9]{2}[ ][0-9]{2}[:][0-9]{2}[:][0-9]{2}"

        matches = re.finditer(utc_pattern, sample_record)
        for match in matches:
            #
            # Turn the string to a list to modify it.
            #
            sample_record = sample_record.replace(
                match.group(0), match.group(0).replace(" ", "-")
            )

        return sample_record

    def __read_records(self):
        """Read and interpret the records of an ORBNUM file.

        Read an orbnum file and set the number of records attribute,
        the length of the records attribute, determine which lines have blank
        records and, perform simple checks of the records.

        :return: header line
        :rtype: str
        """
        blank_records = []

        with open(self.path, "r") as o:
            previous_orbit_number = None
            records = 0
            lines = 0
            header_start = int(self._orbnum_type["header_start_line"])
            for line in o:
                lines += 1
                if lines > header_start + 1:
                    orbit_number = int(line.split()[0])
                    records_length = utf8len(line)

                    #
                    # Checks are performed from the first record.
                    #
                    if lines > header_start:
                        if previous_orbit_number and (
                            orbit_number - previous_orbit_number != 1
                        ):
                            logging.warning(
                                f"-- Orbit number "
                                f"{previous_orbit_number} record "
                                f"is followed by {orbit_number}."
                            )
                        if not line.strip():
                            error_message(
                                f"Orbnum record number {line} is blank.",
                                setup=self.setup,
                            )
                        elif (
                            line.strip() and records_length != self.record_fixed_length
                        ):
                            logging.warning(
                                f"-- Orbit number {orbit_number} "
                                f"record has an incorrect length,"
                                f" the record will be expanded "
                                f"to cover the adequate fixed "
                                f"length."
                            )

                            blank_records.append(str(orbit_number))

                    if line.strip():
                        records += 1

                    previous_orbit_number = orbit_number

        #
        # If there are blank records, we need to add blank spaces and
        # generate a new version of the orbnum file.
        #
        if blank_records:

            #
            # Generate the name for the new orbnum file. This is the only
            # place where the version of the previous orbnum file is used.
            #
            matches = re.search(r"_[vV][0-9]*[\.]", self.name)

            if not matches:

                #
                # If the name does not have an explicit version, the version
                # is set to 2.
                #
                original_name = self.name
                name = self.name.split(".")[0] + "_v2." + self.name.split(".")[-1]
                path = f"{os.sep}".join(self.path.split(os.sep)[:-1]) + os.sep + name

                logging.warning(
                    f"-- ORBNUM file name updated with explicit "
                    f"version number to: {name}"
                )

            else:
                version = re.findall(r"\d+", matches.group(0))
                version_number = "".join(version)

                new_version_number = int(version_number) + 1
                leading_zeros = len(version_number) - len(str(new_version_number))
                new_version_number = str(new_version_number).zfill(leading_zeros)

                new_version = matches.group(0).replace(
                    version_number, new_version_number
                )

                original_name = matches.string
                name = self.name.replace(matches.group(0), new_version)
                path = self.path.replace(matches.group(0), new_version)

                logging.warning(f"-- Orbnum name updated to: {name}")

            #
            # The updated name needs to be propagated to the kernel
            # list.
            #
            for index, item in enumerate(self.kernels_collection.list.kernel_list):
                if item == original_name:
                    self.kernels_collection.list.kernel_list[index] = name

            #
            # Following the name, write the new file and remove the
            # provided orbnum file.
            #
            with open(self.path, "r") as o:
                with open(path, "w") as n:
                    i = 1
                    for line in o:
                        if i > int(header_start) + 1:
                            orbit_number = int(line.split()[0])
                            if str(orbit_number) in blank_records:
                                #
                                # Careful, Python will change the EOL to
                                # Line Feed when reading the file.
                                #
                                line = line.split("\n")[0]
                                n.write(
                                    line
                                    + " " * (self.record_fixed_length - len(line) - 1)
                                    + self.setup.eol
                                )
                            else:
                                n.write(line.split("\n")[0] + self.setup.eol)
                        else:
                            n.write(line.split("\n")[0] + self.setup.eol)
                        i += 1
                os.remove(self.path)

            self.path = path
            self.name = name

        self.blank_records = blank_records
        self.records = records

        return records

    def __set_event_detection_key(self, header):
        """Obtain the ORBNUM event detection key.

        The event detection key is a string identifying which geometric event
        signifies the start of an orbit. The possible events are:

           ``APO``      signals a search for apoapsis

           ``PERI``     signals a search for periapsis

           ``A-NODE``   signals a search for passage through
                        the ascending node

           ``D-NODE``   signals a search for passage through
                        the descending node

           ``MINLAT``   signals a search for the time of
                        minimum planetocentric latitude

           ``MAXLAT``   signals a search for the time of
                        maximum planetocentric latitude

           ``MINZ``     signals a search for the time of the
                        minimum value of the Z (Cartesian)
                        coordinate

           ``MAXZ``     signals a search for the time of the
                        maximum value of the Z (Cartesian)
                        coordinate
        """
        events = ["APO", "PERI", "A-NODE", "D-NODE", "MINLAT", "MAXLAT", "MINZ", "MAXZ"]
        for event in events:
            if header[0].count(event) >= 2:
                self._event_detection_key = event
                break

        #
        # For orbnum files generated with older versions of the ORBNUM
        # utility, the event colums have a different format. For example,
        # for Odissey (m01_ext64.nrb):
        #
        #  No.      Desc-Node UTC         Node SCLK          Asc-Node UTC  ...
        # ===== ====================  ================  ====================
        # 82266 2020 JUL 01 01:24:31  4/1278034592.076  2020 JUL 01 02:23:12
        # 82267 2020 JUL 01 03:23:06  4/1278041706.241  2020 JUL 01 04:21:46
        # 82268 2020 JUL 01 05:21:38  4/1278048819.054  2020 JUL 01 06:20:18
        #
        if not hasattr(self, "_event_detection_key"):
            if ("Desc-Node" in header[0]) or ("Asc-Node" in header[0]):
                desc_node_index = header[0].index("Desc-Node")
                asc_node_index = header[0].index("Asc-Node")
                if asc_node_index < desc_node_index:
                    self._event_detection_key = "A-NODE"
                else:
                    self._event_detection_key = "D-NODE"

        if not hasattr(self, "_event_detection_key"):
            error_message("orbnum event detection key is incorrect", setup=self.setup)

        return None

    def __event_mapping(self, event):
        """Maps the event keyword to the event name/description.

        :return: event description
        :rtype: str
        """
        event_dict = {
            "APO": "apocenter",
            "PERI": "pericenter",
            "A-NODE": "passage through the ascending node",
            "D-NODE": "passage through the descending node",
            "MINLAT": "minimum planetocentric latitude",
            "MAXLAT": "maximum planetocentric latitude",
            "MINZ": "minimum value of the cartesian Z coordinate",
            "MAXZ": "maximum value of the cartesian Z coordinate",
        }

        return event_dict[event]

    def __opposite_event_mapping(self, event):
        """Maps the event keyword to the opposite event keyword.

        :return: opposite event keyword
        :rtype: str
        """
        opp_event_dict = {
            "APO": "PERI",
            "PERI": "APO",
            "A-NODE": "D-NODE",
            "D-NODE": "A-NODE",
            "MINLAT": "MAXLAT",
            "MAXLAT": "MINLAT",
            "MINZ": "MAXZ",
            "MAXZ": "MINZ",
        }

        return opp_event_dict[event]

    def __get_params(self, header):
        """Obtain the parameters present in the ORBNUM file.

        Currently There are 11 orbital parameters available:

           ``No.``          The orbit number of a descending node event.

           ``Event UTC``    The UTC time of that event.

           `Event SCLK``    The SCLK time of the event.

           ``OP-Event UTC`` The UTC time of the opposite event.

           ``Sub Sol Lon``  Sub-solar planetodetic longitude at event
                            time (DEGS).

           ``Sub Sol Lat``  Sub-solar planetodetic latitude at event
                            time (DEGS).

           ``Sub SC Lon``   Sub-target planetodetic longitude (DEGS).

           ``Sub SC Lat``   Sub-target planetodetic latitude (DEGS).

           ``Alt``          Altitude of the target above the observer
                            body at event time (KM).

           ``Inc``          Inclination of the vehicle orbit plane at
                            event time (DEGS).

           ``Ecc``          Eccentricity of the target orbit about
                            the primary body at event time (DEGS),

           ``Lon Node``     Longitude of the ascending node of the
                            orbit plane at event time (DEGS).

           ``Arg Per``      Argument of periapsis of the orbit plane at
                            event time (DEGS).

           ``Sol Dist``     Solar distance from target at event
                            time (KM).

           ``Semi Axis``   Semi-major axis of the target's orbit at
                            event time (KM).
        """
        parameters = []
        params = [
            "No.",
            "Event UTC",
            "Desc-Node UTC",
            "Event SCLK",
            "Node SCLK",
            "OP-Event UTC",
            "Asc-Node UTC",
            "SolLon",
            "SolLat",
            "SC Lon",
            "SC Lat",
            "Alt",
            "Inc",
            "Ecc",
            "LonNode",
            "Arg Per",
            "Sol Dist",
            "Semi Axis",
        ]
        for param in params:
            if param in header[0]:
                parameters.append(param)

        self._params = parameters

        return None

    def __set_params(self, header):
        """Define the parameters template dictionary.

        :param header:
        :return:
        """
        # The orbit number, UTC date and angular parameters have fixed
        # lengths, which are provided in the parameters template.
        # The length of the rest of the parameters depends on each orbnum
        # file and are obtained from the orbum file itself.
        #
        params_template = {
            "No.": {
                "location": "1",
                "type": "ASCII_Integer",
                "length": "5",
                "format": "I5",
                "description": "Number that provides an incremental orbit "
                "count determined by the $EVENT event.",
            },
            "Event UTC": {
                "type": "ASCII_String",
                "location": "8",
                "length": "20",
                "format": "A20",
                "description": "UTC time of the $EVENT event that "
                "signifies the start of an orbit.",
            },
            "Desc-Node UTC": {
                "type": "ASCII_String",
                "location": "8",
                "length": "20",
                "format": "A20",
                "description": "UTC time of the $EVENT event that "
                "signifies the start of an orbit.",
            },
            "Event SCLK": {
                "type": "ASCII_String",
                "format": "A",
                "description": "SCLK time of the $EVENT event that "
                "signifies the start of an orbit.",
            },
            "Node SCLK": {
                "type": "ASCII_String",
                "format": "A",
                "description": "SCLK time of the $EVENT event that "
                "signifies the start of an orbit.",
            },
            "OP-Event UTC": {
                "type": "ASCII_String",
                "length": "20",
                "format": "A20",
                "description": "UTC time of opposite event ($OPPEVENT).",
            },
            "Asc-Node UTC": {
                "type": "ASCII_String",
                "length": "20",
                "format": "A20",
                "description": "UTC time of opposite event ($OPPEVENT).",
            },
            "SolLon": {
                "type": "ASCII_Real",
                "format": "F",
                "description": "Sub-solar planetodetic longitude at the "
                "$EVENT event time in the $FRAME.",
                "unit": "deg",
            },
            "SolLat": {
                "type": "ASCII_Real",
                "format": "F",
                "description": "Sub-solar planetodetic latitude at the "
                "$EVENT event time in the $FRAME.",
                "unit": "deg",
            },
            "SC Lon": {
                "type": "ASCII_Real",
                "format": "F",
                "description": "Sub-target planetodetic longitude at the "
                "$EVENT event time in the $FRAME.",
                "unit": "deg",
            },
            "SC Lat": {
                "type": "ASCII_Real",
                "format": "F",
                "description": "Sub-target planetodetic latitude at at the "
                "$EVENT event time in the $FRAME.",
                "unit": "deg",
            },
            "Alt": {
                "type": "ASCII_Real",
                "format": "F",
                "description": "Altitude of the target above the observer "
                "body at the $EVENT event time relative to"
                " the $TARGET ellipsoid.",
                "unit": "km",
            },
            "Inc": {
                "type": "ASCII_Real",
                "format": "F",
                "description": "Inclination of the vehicle orbit plane at "
                "event time.",
                "unit": "km",
            },
            "Ecc": {
                "type": "ASCII_Real",
                "format": "F",
                "description": "Eccentricity of the target orbit about "
                "the primary body at the $EVENT event time.",
                "unit": "deg",
            },
            "LonNode": {
                "type": "ASCII_Real",
                "format": "F",
                "description": "Longitude of the ascending node of the"
                " orbit plane at the $EVENT event time.",
                "unit": "deg",
            },
            "Arg Per": {
                "type": "ASCII_Real",
                "format": "F",
                "description": "Argument of periapsis of the orbit plane at "
                "the $EVENT event time.",
                "unit": "deg",
            },
            "Sol Dist": {
                "type": "ASCII_Real",
                "format": "F",
                "description": "Solar distance from target at the $EVENT "
                "event time.",
                "unit": "km",
            },
            "Semi Axis": {
                "type": "ASCII_Real",
                "format": "F",
                "description": "Semi-major axis of the target's orbit at"
                " the $EVENT event time.",
                "unit": "km",
            },
        }

        #
        # Define the complete list of parameters.
        #
        params = self._params

        sample_record = self._sample_record

        #
        # Iterate the parameters and generate a dictionary for each based on
        # the parameters template dictionary.
        #
        # The parameters are first initialised. The orbnum header with two
        # re-processed records will look as follows:
        #
        # header[1]     No.     Event UTC PERI       Event SCLK PERI      ...
        # header[2]    =====  ====================  ====================  ...
        # param_record  1954  2015-OCT-01T01:33:57    2/0496935200.34677  ...
        #               1955  2015-OCT-01T06:06:54    2/0496951577.40847  ...
        #
        # The length of each parameter is based on the second header line.
        # Please note that what will be used is the maximum potential length
        # of each parameter.
        #
        number = 0
        location = 1
        length = 0
        params_dict = {}
        blankspace_iter = re.finditer(r"[ ]", header[1])

        for param in params:

            name = param
            #
            # Parameter location; offset with respect to the line start.
            # Use the length from the previous iteration and add the
            # blank spaces in between parameter columns.
            #
            # To find blank spaces we use a regular expression iterator.
            #
            if number > 0:
                blankspaces_loc = blankspace_iter.__next__().regs[0]
                blankspaces = blankspaces_loc[1] - blankspaces_loc[0] + 1
                location += int(length) + blankspaces

            type = params_template[param]["type"]

            if "length" in params_template[param]:
                length = params_template[param]["length"]
                format = params_template[param]["format"]
            else:
                #
                # Obtain the parameter length as the length of the table
                # separator from the header.
                #
                length = str(len(header[1].split()[number]))
                format = params_template[param]["format"] + length

                #
                # If the parameter is a float we need to include the decimal
                # part.
                #
                if "F" in params_template[param]["format"]:
                    #
                    # We need to sort the number of decimals.
                    #
                    param_record = sample_record.split()[number]
                    param_length = str(len(param_record.split(".")[-1]))
                    format += "." + param_length
            #
            # Parameter number (column number)
            #
            number += 1

            #
            # Description. It can contain $EVENT, $TARGET and/or $FRAME. If
            # so is substituted by the appropriate value.
            #
            description = params_template[param]["description"]

            if "$EVENT" in description:
                event_desc = self.__event_mapping(self._event_detection_key)
                description = description.replace("$EVENT", event_desc)

            if "$TARGET" in description:
                target = self.setup.target.title()
                description = description.replace("$TARGET", target)

            if "$FRAME" in description:
                frame_dict = self._orbnum_type["event_detection_frame"]
                frame = f"{frame_dict['description']} ({frame_dict['spice_name']})"
                description = description.replace("$FRAME", frame)

            if "$OPPEVENT" in description:
                #
                # For the opposite event correct the field description as
                # well.
                #
                oppevent = self.__opposite_event_mapping(self._event_detection_key)
                oppevent_desc = self.__event_mapping(oppevent)
                description = description.replace("$OPPEVENT", oppevent_desc)

            #
            # Add event type in names.
            #
            if (name == "Event UTC") or (name == "Event SCLK"):
                name += " " + self._event_detection_key
            if name == "OP-Event UTC":
                name += " " + oppevent

            #
            # If the parameter has a unit, get it from the template.
            #
            if "unit" in params_template[param]:
                unit = params_template[param]["unit"]
            else:
                unit = None

            #
            # build the dictionary for the parameter and add it to the
            # parameter list
            #
            params_dict[param] = {
                "name": name,
                "number": number,
                "location": location,
                "type": type,
                "length": length,
                "format": format,
                "description": description,
                "unit": unit,
            }

        self.params = params_dict

        return None

    def __get_description(self):
        """Write the ORBNUM table character description.

        Write the orbnum product description information based on the orbit
        determination event and the PCK kernel used.
        """
        event_mapping = {
            "PERI": "periapsis",
            "APO": "apoapsis",
            "A-NODE": "ascending node",
            "D-NODE": "descending node",
            "MINLAT": "minimum planetocentric latitude",
            "MAXLAT": "maximum planetocentric latitude",
            "MINZ": "minimum value of Z (cartesian) coordinate",
            "MAXZ": "maximum value of Z (cartesian) coordinate",
        }

        event = event_mapping[self._event_detection_key]

        pck_mapping = self._orbnum_type["pck"]
        report = f"{pck_mapping['description']} ({pck_mapping['kernel_name']})"

        description = (
            f"SPICE text orbit number file containing orbit "
            f"numbers and start times for orbits numbered by/"
            f"starting at {event} events, and sets of selected "
            f"geometric parameters at the orbit start times. "
            f"SPICE text PCK file constants from the {report}."
        )
        if hasattr(self, "_previous_orbnum"):
            if self._previous_orbnum:
                description += (
                    f" This file supersedes the following orbit "
                    f"number file: {self._previous_orbnum}."
                )

        description += f" Created by {self._orbnum_type['author']}."

        return description

    def __table_character_description(self):
        """Write the ORBNUM table character description.

        Write the orbnum table character description information
        determination event and the PCK kernel used.
        """
        description = ""

        if self.blank_records:
            number_of_records = len(self.blank_records)
            if int(number_of_records) == 1:
                plural = ""
            else:
                plural = "s"

            description += (
                f"Since the SPK file(s) used to "
                f"generate this orbit number file did not provide "
                f"continuous coverage, the file contains "
                f"{number_of_records} record{plural} that only provide the "
                f"orbit number in the first field (No.) with all other "
                f"fields set to blank spaces."
            )

        return description

    def __coverage(self):
        """Determine the coverage of the ORNBUM file.

        The coverage of the orbnum file can be determined in three different
        ways:

           *  If there is a one to one correspondence with an SPK
              file, the SPK file can be provided with the <kernel>
              tag. The tag can be a path to a specific kernel that
              does not have to be part of the increment, a pattern
              of a kernel present in the increment or a pattern of
              a kernel present in the final directory of the archive.

           *  If there is a quasi one to one correspondence with an
              SPK file with a given cutoff time prior to the end
              of the SPK file, the SPK file can be provided with the
              <kernel> tag. The tag can be a path to a specific kernel
              that does not have to be part of the increment, a pattern
              of a kernel present in the increment or a pattern of
              a kernel present in the final directory of the archive.
              Currently the only cutoff pattern available is the
              boundary of the previous day of the SPK coverage stop
              time.

           *  A user can provide a look up table with this file, as follows::

                <lookup_table>
                   <file name="maven_orb_rec_210101_210401_v1.orb">
                      <start>2021-01-01T00:00:00.000Z</start>
                      <finish>2021-04-01T01:00:00.000Z</finish>
                   </file>
                </lookup_table>

              Note that in this particular case the first three and
              last three lines of the orbnum files would have provided::

                   Event UTC PERI
                   ====================
                   2021 JAN 01 00:14:15
                   2021 JAN 01 03:50:43
                   2021 JAN 01 07:27:09
                   (...)
                   2021 MAR 31 15:00:05
                   2021 MAR 31 18:36:29
                   2021 MAR 31 22:12:54

           *  If nothing is provided NPB will provide the coverage based on
              the event time of the first orbit and the opposite event time
              of the last orbit.
        """
        if "coverage" in self._orbnum_type:
            coverage_source = self._orbnum_type["coverage"]
        else:
            coverage_source = ""

        coverage_found = False

        if "kernel" in coverage_source:
            if coverage_source["kernel"]:
                coverage_kernel = coverage_source["kernel"]["#text"]
                #
                # Search the kernel that provides coverage, Note that
                # this kernel is either
                #
                #   -- present in the increment
                #   -- in the previous increment
                #   -- provided by the user
                #
                # The kernel can be a pattern defining multiple kernels or
                # a name. If the kernel provided contains a pattern it will be
                # useful to determine the coverage of multiple orbnum files
                # provided in the plan.
                #
                cov_patn = coverage_kernel.split(os.sep)[-1]

                #
                # Start checking the path provided in the configuration file.
                #
                cov_path = os.sep.join(coverage_kernel.split(os.sep)[:-1])

                try:
                    cov_kers = [
                        x for x in os.listdir(cov_path) if re.fullmatch(cov_patn, x)
                    ]

                except BaseException:
                    cov_kers = []

                #
                # Check if the SPK kernel is present in the increment.
                #
                if not cov_kers:
                    cov_path = f"{self.setup.staging_directory}/spice_kernels/spk"

                    try:
                        cov_kers = [
                            x for x in os.listdir(cov_path) if re.fullmatch(cov_patn, x)
                        ]
                    except BaseException:
                        cov_kers = []

                    #
                    # Check if the SPK kernel is present in the bundle
                    # directory.
                    #
                    if not cov_kers:
                        cov_path = f"{self.setup.bundle_directory}/spice_kernels/spk"
                        try:
                            cov_kers = [
                                x
                                for x in os.listdir(cov_path)
                                if re.fullmatch(cov_patn, x)
                            ]
                        except BaseException:
                            cov_kers = []

                if cov_kers:
                    coverage_found = True

                #
                # If there is only one coverage kernel this is the one that
                # will be used.
                #
                if len(cov_kers) == 1 and coverage_found:
                    coverage_kernel = cov_path + os.sep + cov_kers[0]
                    logging.info(f"-- Coverage determined by {coverage_kernel}.")
                #
                # If there are more, the only possibility is that the orbnum
                # filename and the SPK filename, both without extensions, match.
                #
                elif coverage_found:
                    for kernel in cov_kers:
                        kername = kernel.split(os.sep)[-1]
                        if kername.split(".")[0] == self.name.split(".")[0]:
                            coverage_kernel = cov_path + os.sep + kernel
                            logging.info(
                                f"-- Coverage determined by {coverage_kernel}."
                            )

                if os.path.isfile(coverage_kernel):
                    #
                    # Kernel provided by the user and directly available.
                    #
                    (start_time, stop_time) = spk_coverage(
                        coverage_kernel,
                        main_name=self.setup.spice_name,
                        date_format="maklabel",
                    )

                    #
                    # If the XML tag has a cutoff attribute, apply the cutoff.
                    #
                    if coverage_source["kernel"]["@cutoff"] == "True":
                        stop_time = datetime.datetime.strptime(
                            stop_time, "%Y-%m-%dT%H:%M:%SZ"
                        )
                        stop_time = stop_time.strftime("%Y-%m-%dT00:00:00Z")
                    elif coverage_source["kernel"]["@cutoff"] == "False":
                        pass
                    else:
                        logging.error(
                            "-- cutoff value of <kernel>"
                            "configuration item is not set to "
                            'a parseable value: "True" or "False".'
                        )
                    coverage_found = True
                else:
                    coverage_found = False
            else:
                coverage_found = False
        elif "lookup_table" in coverage_source:
            if coverage_source["lookup_table"]:
                if coverage_found:
                    logging.warning(
                        "-- Orbnum file lookup table cov. found "
                        "but cov. already provided by SPK file."
                    )

                table_files = coverage_source["lookup_table"]["file"]
                if not isinstance(table_files, list):
                    table_files = [table_files]
                for file in table_files:
                    if file["@name"] == self.name:
                        start_time = file["start"]
                        stop_time = file["finish"]
                        coverage_found = True
                        logging.info(
                            f"-- Coverage determined by "
                            f'{file["@name"]} from lookup table.'
                        )
                        break
            else:
                coverage_found = False

        if not coverage_found:
            #
            # Set the start and stop times to the first and last
            # time-tags of the orbnum file.
            #
            start_time = self._sample_record.split()[1]
            start = datetime.datetime.strptime(start_time, "%Y-%b-%d-%H:%M:%S")
            start_time = start.strftime("%Y-%m-%dT%H:%M:%SZ")

            #
            # Read the orbnum file in binary mode in order to start
            # from the end of the file.
            #
            with open(self.path, "rb") as f:
                f.seek(-2, os.SEEK_END)
                while f.read(1) != b"\n":
                    f.seek(-2, os.SEEK_CUR)
                last_line = f.readline().decode()
                #
                # Replace the spaces in the UTC strings for dashes in order
                # to be able to split the file with blank spaces.
                #
            last_line = self.__utc_blanks_to_dashes(last_line)
            #
            # If the opposite event is outside of the coverage of the SPK file
            # with which the orbnum has been generated, there is no UTC time
            # or the opposite event, in such case we will use the UTC time
            # of the last event.
            #
            if "Unable to determine" in last_line:
                stop_time = last_line.split()[1]
            else:
                stop_time = last_line.split()[3]

            try:
                stop = datetime.datetime.strptime(stop_time, "%Y-%b-%d-%H:%M:%S")
                stop_time = stop.strftime("%Y-%m-%dT%H:%M:%SZ")
            except BaseException:
                #
                # Exception to cope with orbnum files without all the ground
                # set of parameters.
                #
                stop_time = last_line.split()[1]
                stop = datetime.datetime.strptime(stop_time, "%Y-%b-%d-%H:%M:%S")
                stop_time = stop.strftime("%Y-%m-%dT%H:%M:%SZ")

            logging.warning("-- Coverage determined by ORBNUM file data.")

        self.start_time = start_time
        self.stop_time = stop_time

        return None


class InventoryProduct(Product):
    """Product child Class that defines a Collection Inventory product."""

    def __init__(self, setup, collection):
        """Constructor."""
        line = f"Step {setup.step} - Generation of {collection.name} collection"
        logging.info("")
        logging.info(line)
        logging.info("-" * len(line))
        logging.info("")
        setup.step += 1
        if not setup.args.silent and not setup.args.verbose:
            print("-- " + line.split(" - ")[-1] + ".")

        self.setup = setup
        self.collection = collection

        if setup.pds_version == "3":
            self.path = setup.bundle_directory + os.sep + "index" + os.sep + "index.tab"

        elif setup.pds_version == "4":

            #
            # Determine the inventory version
            #
            if self.setup.increment:
                inventory_files = glob.glob(
                    self.setup.bundle_directory
                    + f"/{self.setup.mission_acronym}_spice"
                    + os.sep
                    + collection.name
                    + os.sep
                    + f"collection_{collection.name}_inventory_v*.csv"
                )
                inventory_files += glob.glob(
                    self.setup.staging_directory
                    + os.sep
                    + collection.name
                    + os.sep
                    + f"collection_{collection.name}_inventory_v*.csv"
                )
                inventory_files.sort()
                try:
                    latest_file = inventory_files[-1]

                    #
                    # We store the previous version to use it to validate the
                    # generated one.
                    #
                    self.path_current = latest_file

                    latest_version = latest_file.split("_v")[-1].split(".")[0]
                    self.version = int(latest_version) + 1

                    logging.info(f"-- Previous inventory file is: {latest_file}")
                    logging.info(f"-- Generate version {self.version}.")

                except BaseException:
                    self.version = 1
                    self.path_current = ""

                    logging.warning("-- Previous inventory file not found.")
                    logging.warning(f"-- Default to version {self.version}.")
                    logging.warning("-- The version of this file might be incorrect.")

            else:
                self.version = 1
                self.path_current = ""

                logging.warning(f"-- Default to version {self.version}.")
                logging.warning(
                    "-- Make sure this is the first release of the archive."
                )

            self.name = f"collection_{collection.name}_inventory_v{self.version:03}.csv"
            self.path = (
                setup.staging_directory + os.sep + collection.name + os.sep + self.name
            )

            self.set_product_lid()
            self.set_product_vid()

        #
        # Kernels are already generated products but Inventories are not.
        #
        self.write_product()
        self.new_product = True
        if setup.pds_version == "4":
            Product.__init__(self)
            self.label = InventoryPDS4Label(setup, collection, self)

        # elif setup.pds_version == '3':
        #    self.label = InventoryPDS3Label(setup, collection, self)

        return

    def set_product_lid(self):
        """Set the Product LID."""
        self.lid = f"{self.setup.logical_identifier}:document:spiceds"

    def set_product_vid(self):
        """Set the Product VID."""
        self.vid = "{}.0".format(int(self.version))

    def write_product(self):
        """Write and valudate the Collection inventory."""
        if self.setup.pds_version == "4":
            self.__write_pds4_collection_product()
        else:
            return
            self.__write_pds3_index_product()

        logging.info(
            f"-- Generated {self.path.split(self.setup.staging_directory)[-1]}"
        )
        if not self.setup.args.silent and not self.setup.args.verbose:
            print(f"   * Created {self.path.split(self.setup.staging_directory)[-1]}.")

        self.validate()

        if self.setup.diff:
            self.compare()

        return

    def __write_pds4_collection_product(self):

        #
        # If there is an existing version we need to add the items from
        # the previous version as SECONDARY members
        #
        with open(self.path, "w+") as f:
            if self.path_current:
                with open(self.path_current, "r") as r:
                    for line in r:
                        if "P,urn" in line:
                            #
                            # All primary items in previous version shall
                            # be included as secondary in the new one
                            #
                            line = line.replace("P,urn", "S,urn")
                        line = add_carriage_return(
                            line, self.setup.eol_pds4, self.setup
                        )
                        f.write(line)

            for product in self.collection.product:
                #
                # This conditional is added because miscellaneous inventories
                # are added to the collection before generating the inventory
                # product itself.
                #
                if type(product) != InventoryProduct:
                    if product.new_product:
                        line = f"P,{product.lid}::{product.vid}\r\n"
                        line = add_carriage_return(
                            line, self.setup.eol_pds4, self.setup
                        )
                        f.write(line)

        return

    #    def __write_pds3_index_product(self):
    #
    #        #
    #        # PDS3 INDEX file generation
    #        #
    #
    #        current_index = list()
    #        kernel_list = list()
    #        kernel_directory_list = ["IK", "FK", "SCLK", "LSK", "PCK", "CK", "SPK"]
    #
    #        # In MEX the DSK folder has nothing to export
    #        if os.path.exists(self.mission.bundle_directory + "/DATA/DSK"):
    #            kernel_directory_list.append("DSK")
    #
    #        if os.path.exists(self.mission.bundle_directory + "/DATA/EK"):
    #            kernel_directory_list.append("EK")
    #
    #        # Note that PCK was doubled here. This accounted for a spurious extra line
    #        # n the INDEX.TAB.
    #
    #        if self.mission.increment:
    #            existing_index = self.mission.increment + "/INDEX/INDEX.TAB"
    #
    #            with open(existing_index, "r") as f:
    #
    #                for line in f:
    #
    #                    if line.strip() == "":
    #                        break
    #
    #                    current_index.append(
    #                        [
    #                            line.split(",")[0].replace(" ", ""),
    #                            line.split(",")[1].replace(" ", ""),
    #                            line.split(",")[2],
    #                            line.split(",")[3].replace("\n", "\r\n"),
    #                        ]
    #                    )
    #                    line = line.split(",")[1]
    #                    line = line[1:-1].rstrip()
    #                    kernel_list.append(line)
    #
    #        new_index = []
    #
    #        for directory in kernel_directory_list:
    #
    #            data_files = self.mission.bundle_directory + "/data/" + directory
    #
    #            for file in os.listdir(data_files):
    #
    #                if file.split(".")[1] != "LBL" and file not in kernel_list:
    #                    new_label_element = (
    #                        '"DATA/' + directory + "/" + file.split(".")[0] + '.LBL"'
    #                    )
    #                    new_kernel_element = '"' + file + '"'
    #
    #                    generation_date = PDS3_label_gen_date(
    #                        data_files + "/" + file.split(".")[0] + ".LBL"
    #                    )
    #                    if "T" not in generation_date:
    #                        generation_date = generation_date
    #
    #                    new_index.append(
    #                        [
    #                            new_label_element,
    #                            new_kernel_element,
    #                            generation_date,
    #                            '"' + self.mission.dataset + '"\r\n',
    #                        ]
    #                    )
    #
    #        #
    #        # Merge both lists
    #        #
    #        index = current_index + new_index
    #
    #        #
    #        # Sort out which is the kernel that has the most characters
    #        # and we add blank spaces to the rest
    #        #
    #        lab_filenam_list = list()
    #        ker_filenam_list = list()
    #
    #        for element in index:
    #            lab_filenam_list.append(element[0])
    #            ker_filenam_list.append(element[1])
    #
    #        longest_lab_name = max(lab_filenam_list, key=len)
    #        max_lab_name_len = len(longest_lab_name)
    #
    #        longest_ker_name = max(ker_filenam_list, key=len)
    #        max_ker_name_len = len(longest_ker_name)
    #
    #        index_list = list()
    #        dates = []  # used to sort out according to generation date the index_list
    #        for element in index:
    #            blanks = max_lab_name_len - (len(element[0]))
    #            label = element[0][:-1] + " " * blanks + element[0][-1]
    #
    #            blanks = max_ker_name_len - (len(element[1]))
    #            kernel = element[1][:-1] + " " * blanks + element[1][-1]
    #            if "\n" in element[-1]:
    #                index_list.append(
    #                    label + "," + kernel + "," + element[2] + "," + element[3]
    #                )
    #            else:
    #                index_list.append(
    #                    label + "," + kernel + "," + element[2] + "," + element[3] + "\n"
    #                )
    #            dates.append(element[2])
    #        with open(self.mission.bundle_directory + "/index/index.tab", "w+") as f:
    #            for element in [x for _, x in sorted(zip(dates, index_list))]:
    #                f.write(element)
    #
    #        return

    def validate(self):
        """Validate the Inventory against previous versions.

        The Inventory is validated by checking that all the products are present
        and by comparing the Inventory Product with the previous version and
        if it does not exist with the sample inventory product.
        """
        logging.info(f"-- Validating {self.name}...")

        #
        # Check that all the products are listed in the collection product.
        #
        logging.info("      Check that all the products are in the collection.")

        for product in self.collection.product:
            if type(product).__name__ != "InventoryProduct":
                product_found = False
                with open(self.path, "r") as c:
                    for line in c:
                        if product.lid in line:
                            product_found = True
                    if not product_found:
                        logging.error(
                            f"      Product {product.lid} not found. "
                            f"Consider increment re-generation."
                        )

        logging.info("      OK")
        logging.info("")

    def compare(self):
        """Compare the label with the previous version.

        The label is compared the Inventory Product with the previous
        version and if it does not exist with the sample inventory product.

        :return:
        """
        mission_acronym = self.setup.mission_acronym
        logging.info(
            f"-- Comparing "
            f'{self.name.split(f"{mission_acronym}_spice/")[-1]}'
            f"..."
        )

        #
        # Use the prior version of the same product, if it does not
        # exist use the sample.
        #
        if self.path_current:

            fromfile = self.path_current
            tofile = self.path
            dir = self.setup.working_directory

            compare_files(fromfile, tofile, dir, self.setup.diff)

        else:

            logging.warning("-- Comparing with InSight test inventory product.")
            fromfiles = glob.glob(
                f'{self.setup.root_dir.replace("src/pds","tests")}'
                f"/data/regression/"
                f"insight_spice/{self.collection.type}/"
                f"collection_{self.collection.name}"
                f"_inventory_*.csv"
            )
            fromfiles.sort()
            fromfile = fromfiles[-1]
            tofile = self.path
            dir = self.setup.working_directory

            compare_files(fromfile, tofile, dir, self.setup.diff)

        logging.info("")

        return

    def write_index(self):
        """Write PDS3 index file."""
        line = f"Step {self.setup.step} - Generation of index files"
        logging.info("")
        logging.info(line)
        logging.info("-" * len(line))
        logging.info("")
        self.setup.step += 1
        if not self.setup.args.silent and not self.setup.args.verbose:
            print("-- " + line.split(" - ")[-1] + ".")

        cwd = os.getcwd()
        os.chdir(self.setup.staging_directory)

        list = f"{self.setup.working_directory}/{self.collection.list.complete_list}"
        command = f"perl {self.setup.root_dir}exe/xfer_index.pl {list}"
        logging.info(f"-- Executing: {command}")

        command_process = subprocess.Popen(
            command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )

        process_output, _ = command_process.communicate()
        text = process_output.decode("utf-8")

        os.chdir(cwd)

        for line in text.split("\n"):
            logging.info("   " + line)

        #
        # The Perl script returns an error message if there is a problem.
        #
        if ("ERROR" in text) or ("command not found" in text):
            error_message(text, setup=self.setup)

        #
        # Move index files to appropriate directory
        #
        index = f"{self.setup.staging_directory}/index.tab"
        index_lbl = f"{self.setup.staging_directory}/index.lbl"
        dsindex = f"{self.setup.staging_directory}/dsindex.tab"
        dsindex_lbl = f"{self.setup.staging_directory}/dsindex.lbl"

        #
        # Add CRS to the index files.
        #

        if self.setup.pds_version == "4":
            os.remove(index)
            os.remove(index_lbl)

            dsindex = shutil.move(dsindex, self.setup.staging_directory + "/../")
            dsindex_lbl = shutil.move(
                dsindex_lbl, self.setup.staging_directory + "/../"
            )

            logging.info("-- Adding CRs to index files.")
            logging.info("")
            add_crs_to_file(dsindex, self.setup.eol_pds4, self.setup)
            add_crs_to_file(dsindex_lbl, self.setup.eol_pds4, self.setup)

            self.index = ""
            self.index_lbl = ""
            self.dsindex = dsindex
            self.dsindex_lbl = dsindex_lbl

        self.validate_index()

        return

    def validate_index(self):
        """Validate the index file and label.

        The index and the index label are validated by comparing with
        the previous versions

        :return:
        """
        #
        # Compare with previous index file, if exists. Otherwise it is
        # not compared.
        #
        indexes = [self.index, self.index_lbl, self.dsindex, self.dsindex_lbl]

        for index in indexes:
            if index:
                try:
                    current_index = (
                        self.setup.bundle_directory + os.sep + index.split(os.sep)[-1]
                    )
                    compare_files(
                        index, current_index, self.setup.working_directory, "all"
                    )
                except BaseException:
                    logging.warning(f"-- File to compare with does not exist: {index}")

        return


class SpicedsProduct(object):
    """Product child class to process the SPICEDS file."""

    def __init__(self, setup, collection):
        """Constructor."""
        self.setup = setup
        self.collection = collection
        self.new_product = True
        try:
            spiceds = self.setup.spiceds
        except BaseException:
            spiceds = ""

        line = f"Step {self.setup.step} - Processing spiceds file"
        logging.info("")
        logging.info(line)
        logging.info("-" * len(line))
        logging.info("")
        self.setup.step += 1
        if not self.setup.args.silent and not self.setup.args.verbose:
            print("-- " + line.split(" - ")[-1] + ".")

        if not spiceds:
            logging.info("-- No spiceds file provided.")

        #
        # Obtain the previous spiceds file if it exists
        #
        path = (
            setup.bundle_directory
            + os.sep
            + setup.mission_acronym
            + "_spice"
            + os.sep
            + collection.name
        )
        if self.setup.increment:
            spiceds_files = glob.glob(path + os.sep + "spiceds_v*.html")
            spiceds_files.sort()
            try:
                latest_spiceds = spiceds_files[-1]
                latest_version = latest_spiceds.split("_v")[-1].split(".")[0]
                self.latest_spiceds = latest_spiceds
                self.latest_version = latest_version
                self.version = int(latest_version) + 1

                if not spiceds:
                    logging.info(f"-- Previous spiceds found: {latest_spiceds}")
                    self.generated = False
                    return

            except BaseException:
                logging.warning("-- No previous version of spiceds_v*.html file found.")
                if not spiceds:
                    error_message(
                        "spiceds not provided and not available "
                        "from previous releases",
                        setup=self.setup,
                    )
                self.version = 1
                self.latest_spiceds = ""
        else:
            self.version = 1
            self.latest_spiceds = ""
            if not spiceds:
                error_message(
                    "spiceds not provided and not available from previous releases",
                    setup=self.setup,
                )

        self.name = "spiceds_v{0:0=3d}.html".format(self.version)
        self.path = (
            setup.staging_directory + os.sep + collection.name + os.sep + self.name
        )
        self.mission = setup

        self.set_product_lid()
        self.set_product_vid()

        logging.info(
            f"-- spiceds file provided as input moved to staging "
            f"area as {self.name}"
        )

        #
        # The provuided spiceds file is moved to the staging area.
        #
        shutil.copy2(spiceds, self.path)

        #
        # The appropriate line endings are provided to the spiceds file,
        # If line endings did not have Carriage Return (CR), they are
        # added and the file timestamp is updated. Note that if the file
        # already had CRs the original timestamp is preserved.
        #
        self.check_cr()

        #
        # Kernels are already generated products but Inventories are not.
        #
        self.new_product = True
        Product.__init__(self)

        #
        # Check if the spiceds has not changed.
        #
        self.generated = self.check_product()

        #
        # Validate the product by comparing it and then generate the label.
        #
        if self.generated:
            if self.setup.diff:
                self.compare()

            self.label = DocumentPDS4Label(setup, collection, self)

        return

    def set_product_lid(self):
        """Set the Product LID."""
        self.lid = f"{self.setup.logical_identifier}:document:spiceds"

    def set_product_vid(self):
        """Set the Product VID."""
        self.vid = "{}.0".format(int(self.version))

    def check_cr(self):
        """Determine whether if ``<CR>`` has to be added to the SPICEDS."""
        #
        # We add the date to the temporary file to have a unique name.
        #
        today = date.today()
        time_string = today.strftime("%Y-%m-%dT%H:%M:%S.%f")
        temporary_file = f"{self.path}.{time_string}"

        with open(self.path, "r") as s:
            with open(temporary_file, "w+") as t:
                for line in s:
                    line = add_carriage_return(line, self.setup.eol_pds4, self.setup)
                    t.write(line)

        #
        # If CRs have been added then we update the spiceds file.
        # The operator is notified.
        #
        if filecmp.cmp(temporary_file, self.path):
            os.remove(temporary_file)
        else:
            shutil.move(temporary_file, self.path)
            logging.info(
                "-- Carriage Return has been added to lines in the spiceds file."
            )

        return

    def check_product(self):
        """Check if the SPICEDS product needs to be generated."""
        #
        # If the previous spiceds document is the same then it does not
        # need to be generated.
        #
        generate_spiceds = True
        if self.latest_spiceds:
            with open(self.path) as f:
                spiceds_current = f.readlines()
            with open(self.latest_spiceds) as f:
                spiceds_latest = f.readlines()

            differ = difflib.Differ(charjunk=difflib.IS_CHARACTER_JUNK)
            diff = list(differ.compare(spiceds_current, spiceds_latest))

            generate_spiceds = False
            for line in diff:
                if line[0] == "-":
                    if (
                        "Last update" not in line
                        and line.strip() != "-"
                        and line.strip() != "-\n"
                    ):
                        generate_spiceds = True

            if not generate_spiceds:
                os.remove(self.path)
                logging.warning("-- spiceds document does not need to be " "updated.")
                logging.warning("")

        return generate_spiceds

    def compare(self):
        """Compare the SPICEDS file with a prior one or the template."""
        #
        # Compare spiceds with latest. First try with previous increment.
        #
        try:

            val_spd_path = (
                f"{self.setup.bundle_directory}/"
                f"{self.setup.mission_acronym}_spice/document"
            )

            val_spds = glob.glob(f"{val_spd_path}/spiceds_v*.html")
            val_spds.sort()
            val_spd = val_spds[-1]

        except BaseException:

            #
            # If previous increment does not work, compare with InSight
            # example.
            #
            logging.warning(f"-- No other version of {self.name} has been found.")
            logging.warning("-- Comparing with default InSight example.")

            val_spd = (
                f'{self.setup.root_dir.replace("src/pds","tests")}'
                f"/data/spiceds_insight.html"
            )

        logging.info("")
        fromfile = val_spd
        tofile = self.path
        dir = self.setup.working_directory

        compare_files(fromfile, tofile, dir, self.setup.diff)

        return


class ReadmeProduct(Product):
    """Product child class to generate the Readme Product."""

    def __init__(self, setup, bundle):
        """Constructor."""
        line = f"Step {setup.step} - Generation of bundle products"
        logging.info("")
        logging.info(line)
        logging.info("-" * len(line))
        logging.info("")
        setup.step += 1
        if not setup.args.silent and not setup.args.verbose:
            print("-- " + line.split(" - ")[-1] + ".")

        self.name = "readme.txt"
        self.bundle = bundle
        self.path = setup.staging_directory + os.sep + self.name
        self.setup = setup
        self.vid = bundle.vid
        self.collection = Object()
        self.collection.name = ""

        path = (
            self.setup.bundle_directory
            + f"/{self.setup.mission_acronym}_spice/readme.txt"
        )

        if os.path.exists(path):
            self.path = path
            logging.info("-- Readme file already exists in final area.")
            self.new_product = False
        else:
            logging.info("-- Generating readme file...")
            self.write_product()
            self.new_product = True

            #
            # If the product is generated we define a checksum attribute for
            # the Bundle object.
            #
            self.bundle.checksum = md5(self.path)

        Product.__init__(self)

        logging.info("")

        #
        # Now we change the path for the difference of the name in the label
        #
        self.path = setup.staging_directory + os.sep + bundle.name

        logging.info("-- Generating bundle label...")
        self.label = BundlePDS4Label(setup, self)

    def write_product(self):
        """Write the Readme product."""
        line_length = 0

        #
        # If the readme file is provided via configuration copy it, otherwise
        # generate it with the tempalte.
        #
        if (not os.path.isfile(self.path)) and ("input" in self.setup.readme):
            if os.path.exists(self.setup.readme["input"]):
                shutil.copy(self.setup.readme["input"], self.path)
            else:
                error_message("Readme file provided via configuration does not exist")
        elif not os.path.isfile(self.path):
            with open(self.path, "w+") as f:
                with open(self.setup.root_dir + "/templates/template_readme.txt", 'r') as t:
                    for line in t:
                        if "$SPICE_NAME" in line:
                            line = line.replace("$SPICE_NAME", self.setup.spice_name)
                            line_length = len(line) - 1
                            line = add_carriage_return(
                                line, self.setup.eol_pds4, self.setup
                            )
                            f.write(line)
                        elif "$UNDERLINE" in line:
                            line = line.replace("$UNDERLINE", "=" * line_length)
                            line_length = len(line) - 1
                            line = add_carriage_return(
                                line, self.setup.eol_pds4, self.setup
                            )
                            f.write(line)
                        elif "$OVERVIEW" in line:
                            overview = self.setup.readme["overview"]
                            for line in overview.split("\n"):
                                line = " " * 3 + line.strip() + "\n"
                                line = add_carriage_return(line, self.setup.eol, self.setup)
                                f.write(line)
                        elif "$COGNISANT_AUTHORITY" in line:
                            cognisant = self.setup.readme["cognisant_authority"]
                            for line in cognisant.split("\n"):
                                line = " " * 3 + line.strip() + "\n"
                                line = add_carriage_return(line, self.setup.eol, self.setup)
                                f.write(line)
                        else:
                            line_length = len(line) - 1
                            line = add_carriage_return(line, self.setup.eol, self.setup)
                            f.write(line)

        logging.info("-- Created readme file.")
        if not self.setup.args.silent and not self.setup.args.verbose:
            print("   * Created readme file.")

        return


class ChecksumProduct(Product):
    """Product child class to generate a Checksum Product."""

    def __init__(self, setup, collection):
        """Constructor."""
        #
        # The initialisation of the checksum class is lighter than the
        # initialisation of the other products because the purpose is
        # solely to obtain the LID and the VID of the checksum in order
        # to be able to include it in the miscellaneous collection
        # inventory file; the checksum file needs to be included in the
        # inventory file before the actual checksum file is generated.
        #
        self.setup = setup
        self.collection = collection
        self.collection_path = (
            self.setup.staging_directory + os.sep + "miscellaneous" + os.sep
        )

        #
        # We generate the kernel directory if not present
        #
        product_path = self.collection_path + "checksum" + os.sep
        safe_make_directory(product_path)

        #
        # Initialise the checksum dictionary; we use a dictionary to be
        # able to sort it by value into a list to generate the checksum
        # table.
        #
        self.md5_dict = {}

        self.read_current_product()

        self.set_product_lid()
        self.set_product_vid()

    def set_coverage(self):
        """Determine the coverage of the Checksum file."""
        #
        # The coverage is set by generating the checksum file but without
        # writing it.
        #
        self.write_product(history=False, set_coverage=True)

    def generate(self, history=False):
        """Write and label the Checksum file.

        :param history:
        :return:
        """
        line = f"Step {self.setup.step} - Generate checksum file"
        logging.info("")
        logging.info(line)
        logging.info("-" * len(line))
        logging.info("")
        self.setup.step += 1
        if not self.setup.args.silent and not self.setup.args.verbose:
            print("-- " + line.split(" - ")[-1] + ".")

        #
        # This acts as the second part of the Checksum product initialization.
        #
        self.write_product(history=history)

        #
        # Call the constructor of the parent class to fill the common
        # attributes.
        #
        self.new_product = True
        Product.__init__(self)

        #
        # The checksum is labeled.
        #
        logging.info(f"-- Labeling {self.name}...")
        self.label = ChecksumPDS4Label(self.setup, self)

    def read_current_product(self):
        """Reads the current checksum file.

        Reads the current checksum file and determine
        the version of the new checksum file.
        """
        #
        # Determine the checksum version
        #
        if self.setup.increment:
            checksum_files = glob.glob(
                self.setup.bundle_directory
                + f"/{self.setup.mission_acronym}_spice/"
                + os.sep
                + self.collection.name
                + os.sep
                + "checksum_v*.tab"
            )

            checksum_files += glob.glob(
                self.setup.staging_directory
                + os.sep
                + self.collection.name
                + "/checksum/checksum_v*.tab"
            )
            checksum_files.sort()
            try:
                latest_file = checksum_files[-1]

                #
                # Store the previous version to use it to validate the
                # generated one.
                #
                self.path_current = latest_file
                self.name_current = latest_file.split(os.sep)[-1]

                latest_version = latest_file.split("_v")[-1].split(".")[0]
                self.version = int(latest_version) + 1

                logging.info(f"-- Previous checksum file is: {latest_file}")
                logging.info(f"-- Generate version {self.version}.")
                logging.info("")

            except BaseException:
                self.version = 1
                self.path_current = ""

                logging.warning("-- Previous checksum file not found.")
                logging.warning(f"-- Default to version {self.version}.")
                logging.warning("-- The version of this file might be incorrect.")

        else:
            self.version = 1
            self.path_current = ""

            logging.warning(f"-- Default to version {self.version}.")
            logging.warning("-- Make sure this is the first release of the archive.")
            logging.warning("")

        self.name = f"checksum_v{self.version:03}.tab"
        self.path = (
            self.setup.staging_directory
            + os.sep
            + self.collection.name
            + os.sep
            + "checksum"
            + os.sep
            + self.name
        )

        #
        # Add each element of current checksum into the md5_sum attribute.
        #
        if self.path_current:
            with open(self.path_current, "r") as c:
                for line in c:
                    #
                    # Check the format of the current checksum file.
                    #
                    try:
                        (md5, filename) = line.split()
                    except BaseException:
                        error_message(
                            f"Checksum file {self.path_current} is corrupted.",
                            setup=self.setup,
                        )

                    if len(md5) == 32:
                        self.md5_dict[md5] = filename
                    else:
                        error_message(
                            f"Checksum file {self.path_current} "
                            f"corrupted entry: {line}",
                            setup=self.setup,
                        )

        self.new_product = True

    def set_product_lid(self):
        """Set Product LID."""
        self.lid = f"{self.setup.logical_identifier}:miscellaneous:checksum_checksum"

    def set_product_vid(self):
        """Set Product VID."""
        self.vid = "{}.0".format(int(self.version))

    def write_product(self, history=False, set_coverage=False):
        """Write the Checksum file and determine its start and stop time.

        This method can also be used to determine the start and stop time of
        the checksum file, necessary to determine these times for the
        miscellaneous collection before the checksum is actually written.

        :param history:
        :param set_coverage:
        :return:
        """
        msn_acr = self.setup.mission_acronym

        #
        # The checksum file of the current run is generated without the
        # bundle history and using the checksum hashes obtained during
        # the pipeline execution.
        #
        if not history:
            #
            # Iterate the collections to obtain the checksum of each product.
            #
            for collection in self.collection.bundle.collections:
                for product in collection.product:
                    product_name = product.path.split(f"/{msn_acr}_spice/")[-1]
                    if hasattr(product, "checksum"):
                        self.md5_dict[product.checksum] = product_name
                    else:
                        #
                        # For the current checksum product that does not have
                        # a checksum.
                        #
                        pass
                    #
                    # Generate the MD5 checksum of the label.
                    #
                    if hasattr(product, "label"):
                        label_checksum = md5(product.label.name)
                        self.md5_dict[label_checksum] = product.label.name.split(
                            f"/{msn_acr}_spice/"
                        )[-1]
                    else:
                        logging.warning(f"-- {product_name} does not have a label.")
                        logging.info("")
            #
            # Include the readme file checksum if it has been generated in
            # this run. This is a bundle level product.
            #
            if hasattr(self.collection.bundle, "checksum"):
                self.md5_dict[self.collection.bundle.checksum] = "readme.txt"

            #
            # Include the bundle label, that is paired to the readme file.
            #
            if not set_coverage:
                label_checksum = md5(self.collection.bundle.readme.label.name)
                self.md5_dict[
                    label_checksum
                ] = self.collection.bundle.readme.label.name.split(
                    f"/{msn_acr}_spice/"
                )[
                    -1
                ]

        #
        # The missing checksum files are generated from the bundle history.
        #
        if history:
            for product in history[1]:
                path = (
                    self.setup.bundle_directory
                    + f"/{self.setup.mission_acronym}_spice/"
                    + product
                )
                #
                # Computing checksums is resource consuming; let's see if the
                # product has the checksum in its label already. Otherwise
                # compute the checksum.
                #
                checksum = ""
                if ".xml" not in product:
                    checksum = checksum_from_registry(
                        path, self.setup.working_directory
                    )
                    if not checksum:
                        checksum = checksum_from_label(path)
                if not checksum:
                    checksum = md5(path)

                self.md5_dict[checksum] = product

        #
        # The resulting dictionary needs to be transformed into a list
        # sorted by filename alphabetical order (second column of the
        # resulting table)
        #
        md5_list = []

        md5_sorted_dict = dict(sorted(self.md5_dict.items(), key=lambda item: item[1]))

        for key, value in md5_sorted_dict.items():
            md5_list.append(f"{key}  {value}")

        #
        # We remove spurious .DS_Store files if we are working with MacOS.
        #
        for root, _dirs, files in os.walk(self.setup.bundle_directory):
            for file in files:
                if file.endswith(".DS_Store"):
                    path = os.path.join(root, file)
                    logging.info(f"-- Removing {file}")
                    os.remove(path)

        #
        # Checksum start and stop times are the same as the ones defined
        # by the spice_kernel collection and the orbnum files in the
        # miscellaneous collection.
        #
        coverage_list = []

        #
        # Gather all the relevant products that define the coverage of the
        # checksum file: orbnum labels and spice_kernels collection labels.
        #
        for product in md5_list:
            if ("spice_kernels/collection_spice_kernels_v" in product) or (
                ("miscellaneous/orbnum/" in product) and ((".xml" in product))
            ):
                coverage_list.append(product.split()[-1])

        start_times = []
        stop_times = []
        for product in coverage_list:
            #
            # The files can either be in the staging or the final area.
            #
            path = (
                f"{self.setup.bundle_directory}/"
                f"{self.setup.mission_acronym}_spice/" + product
            )
            if not os.path.isfile(path):
                path = f"{self.setup.staging_directory}/" + product
                if not os.path.isfile(path):
                    logging.error(
                        f"-- Product required to determine "
                        f"{self.name} coverage: {product} not found."
                    )

            if os.path.isfile(path):
                with open(path, "r") as lbl:
                    for line in lbl:
                        if "<start_date_time>" in line:
                            start_time = line.split("<start_date_time>")[-1].split(
                                "</"
                            )[0]
                            start_times.append(start_time)
                        if "<stop_date_time>" in line:
                            stop_time = line.split("<stop_date_time>")[-1].split("</")[
                                0
                            ]
                            stop_times.append(stop_time)

        if not start_times:
            logging.warning(
                f"-- Start time set to "
                f"mission start time: {self.setup.mission_start}"
            )
            start_times.append(self.setup.mission_start)

        if not stop_times:
            logging.warning(
                f"-- Stop time set to "
                f"mission finish time: {self.setup.mission_finish}"
            )
            stop_times.append(self.setup.mission_finish)

        start_times.sort()
        stop_times.sort()

        self.start_time = start_times[0]
        self.stop_time = stop_times[-1]

        if not set_coverage:
            #
            # Write the checksum file.
            #
            with open(self.path, "w") as c:
                for entry in md5_list:
                    entry = add_carriage_return(entry, self.setup.eol, self.setup)
                    c.write(entry)

            if self.setup.diff:
                logging.info("-- Comparing checksum with previous version...")
                self.compare()

        return None

    def compare(self):
        """Compare, with previous checksum file, if exists."""
        try:
            logging.info("")
            compare_files(
                self.path_current,
                self.path,
                self.setup.working_directory,
                self.setup.diff,
            )
        except BaseException:
            logging.warning("-- Checksum from previous increment does not exist.")

        logging.info("")

        return None