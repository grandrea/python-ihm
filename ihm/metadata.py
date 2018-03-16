"""Classes to extract metadata from various input files.

   Often input files contain metadata that would be useful to include in
   the mmCIF file, but the metadata is stored in a different way for each
   domain-specific file type. For example, MRC files used for electron
   microscopy maps may contain an EMDB identifier, which the mmCIF file
   can point to in preference to the local file.

   This module provides classes for each file type to extract suitable
   metadata where available.
"""

from . import location, dataset, startmodel, util
from .format import CifWriter

import operator
import struct
import json
import sys
import re

# Handle different naming of urllib in Python 2/3
try:
    import urllib.request as urllib2
except ImportError:
    import urllib2

class Parser(object):
    """Base class for all metadata parsers."""

    def parse_file(self, filename):
        """Extract metadata from the given file.

           :param str filename: the file to extract metadata from.
           :return: a dict with extracted metadata (generally including
                    a :class:`~ihm.dataset.Dataset`)."""
        pass


class MRCParser(Parser):
    """Extract metadata from an EM density map (MRC file)."""

    def parse_file(self, filename):
        """Extract metadata. See :meth:`Parser.parse_file` for details.

           :return: a dict with key `dataset` pointing to the density map,
                    as an EMDB entry if the file contains EMDB headers,
                    otherwise to the file itself.
        """
        emdb = self._get_emdb(filename)
        if emdb:
            version, details = self._get_emdb_info(emdb)
            l = location.EMDBLocation(emdb, version=version,
                                      details=details if details
                                         else "Electron microscopy density map")
        else:
            l = location.InputFileLocation(filename,
                                      details="Electron microscopy density map")
        return {'dataset': dataset.EMDensityDataset(l)}

    def _get_emdb_info(self, emdb):
        """Query EMDB API and return version & details of a given entry"""
        req = urllib2.Request('https://www.ebi.ac.uk/pdbe/api/emdb/entry/'
                              'summary/%s' % emdb, None, {})
        response = urllib2.urlopen(req)
        contents = json.load(response)
        keys = list(contents.keys())
        info = contents[keys[0]][0]['deposition']
        # JSON values are always Unicode, but on Python 2 we want non-Unicode
        # strings, so convert to ASCII
        if sys.version_info[0] < 3:
            return (info['map_release_date'].encode('ascii'),
                    info['title'].encode('ascii'))
        else:
            return info['map_release_date'], info['title']

    def _get_emdb(self, filename):
        """Return the EMDB id of the file, or None."""
        r = re.compile(b'EMDATABANK\.org.*(EMD\-\d+)')
        with open(filename, 'rb') as fh:
            fh.seek(220) # Offset of number of labels
            num_labels_raw = fh.read(4)
            # Number of labels in MRC is usually a very small number, so it's
            # very likely to be the smaller of the big-endian and little-endian
            # interpretations of this field
            num_labels_big, = struct.unpack_from('>i', num_labels_raw)
            num_labels_little, = struct.unpack_from('<i', num_labels_raw)
            num_labels = min(num_labels_big, num_labels_little)
            for i in range(num_labels):
                label = fh.read(80).strip()
                m = r.search(label)
                if m:
                    if sys.version_info[0] < 3:
                        return m.group(1)
                    else:
                        return m.group(1).decode('ascii')


class PDBParser(Parser):
    """Extract metadata (e.g. PDB ID, comparative modeling templates) from a
       PDB file. This handles PDB headers added by the PDB database itself,
       comparative modeling packages such as MODELLER and Phyre2, and also
       some custom headers that can be used to indicate that a file has been
       locally modified in some way."""

    def parse_file(self, filename):
        """Extract metadata. See :meth:`Parser.parse_file` for details.

           :param str filename: the file to extract metadata from.
           :return: a dict with key `dataset` pointing to the PDB dataset;
                    'templates' pointing to a list of comparative model
                    templates as :class:`ihm.startmodel.Template` objects;
                    'software' pointing to a dict with keys the name of
                    comparative modeling packages used and values their
                    versions;
                    'metadata' a list of PDB metadata records.
        """
        ret = {'templates':[], 'software':{}, 'metadata':[]}
        with open(filename) as fh:
            first_line = fh.readline()
            local_file = location.InputFileLocation(filename,
                                          details="Starting model structure")
            if first_line.startswith('HEADER'):
                self._parse_official_pdb(fh, first_line, ret)
            elif first_line.startswith('EXPDTA    DERIVED FROM PDB:'):
                self._parse_derived_from_pdb(fh, first_line, local_file,
                                             ret)
            elif first_line.startswith('EXPDTA    DERIVED FROM COMPARATIVE '
                                       'MODEL, DOI:'):
                self._parse_derived_from_model(fh, first_line, local_file, ret)
            elif first_line.startswith('EXPDTA    THEORETICAL MODEL, MODELLER'):
                self._parse_modeller_model(fh, first_line, local_file,
                                           filename, ret)
            elif first_line.startswith('REMARK  99  Chain ID :'):
                self._parse_phyre_model(fh, first_line, local_file,
                                        filename, ret)
            else:
                self._parse_unknown_model(fh, first_line, local_file,
                                          filename, ret)
        return ret

    def _parse_official_pdb(self, fh, first_line, ret):
        """Handle a file that's from the official PDB database."""
        version, details, metadata = self._parse_pdb_records(fh, first_line)
        l = location.PDBLocation(first_line[62:66].strip(), version, details)
        ret['metadata'] = metadata
        ret['dataset'] = dataset.PDBDataset(l)

    def _parse_derived_from_pdb(self, fh, first_line, local_file, ret):
        # Model derived from a PDB structure; treat as a local experimental
        # model with the official PDB as a parent
        local_file.details = self._parse_details(fh)
        db_code = first_line[27:].strip()
        d = dataset.PDBDataset(local_file)
        d.parents.append(dataset.PDBDataset(location.PDBLocation(db_code)))
        ret['dataset'] = d

    def _parse_derived_from_model(self, fh, first_line, local_file, ret):
        # Model derived from a comparative model; link back to the original
        # model as a parent
        local_file.details = self._parse_details(fh)
        d = dataset.ComparativeModelDataset(local_file)
        repo = location.Repository(doi=first_line[46:].strip())
        # todo: better specify an unknown path
        orig_loc = location.InputFileLocation(repo=repo, path='.',
                          details="Starting comparative model structure")
        d.parents.append(dataset.ComparativeModelDataset(orig_loc))
        ret['dataset'] = d

    def _parse_modeller_model(self, fh, first_line, local_file, filename, ret):
        # todo: handle this more cleanly (generate Software object?)
        ret['software']['modeller'] = first_line[38:].split(' ', 1)
        self._handle_comparative_model(local_file, filename, ret)

    def _parse_phyre_model(self, fh, first_line, local_file, filename, ret):
        # Model generated by Phyre2
        # todo: extract Modeller-like template info for Phyre models
        ret['software']['phyre2'] = CifWriter.unknown
        self._handle_comparative_model(local_file, filename, ret)

    def _parse_unknown_model(self, fh, first_line, local_file, filename, ret):
        # todo: revisit assumption that all unknown source PDBs are
        # comparative models
        self._handle_comparative_model(local_file, filename, ret)

    def _handle_comparative_model(self, local_file, pdbname, ret):
        d = dataset.ComparativeModelDataset(local_file)
        ret['dataset'] = d
        ret['templates'] = self._get_templates(pdbname, d)

    def _get_templates(self, pdbname, target_dataset):
        template_path_map = {}
        alnfile = None
        alnfilere = re.compile('REMARK   6 ALIGNMENT: (\S+)')
        tmppathre = re.compile('REMARK   6 TEMPLATE PATH (\S+) (\S+)')
        tmpre = re.compile('REMARK   6 TEMPLATE: '
                           '(\S+) (\S+):(\S+) \- (\S+):\S+ '
                           'MODELS (\S+):\S+ \- (\S+):\S+ AT (\S+)%')
        template_info = []

        with open(pdbname) as fh:
            for line in fh:
                if line.startswith('ATOM'): # Read only the header
                    break
                m = tmppathre.match(line)
                if m:
                    template_path_map[m.group(1)] = \
                              util._get_relative_path(pdbname, m.group(2))
                m = alnfilere.match(line)
                if m:
                    # Path to alignment is relative to that of the PDB file
                    fname = util._get_relative_path(pdbname, m.group(1))
                    alnfile = location.InputFileLocation(fname,
                                        details="Alignment for starting "
                                               "comparative model")
                m = tmpre.match(line)
                if m:
                    template_info.append(m)

        templates = [self._handle_template(t, template_path_map,
                            target_dataset, alnfile) for t in template_info]
        # Sort templates by starting residue, then ending residue
        return(sorted(templates, key=operator.attrgetter('seq_id_range')))

    def _handle_template(self, info, template_path_map, target_dataset,
                         alnfile):
        """Create a Template object from Modeller PDB header information."""
        template_code = info.group(1)
        template_seq_id_range = (int(info.group(2)), int(info.group(4)))
        template_asym_id = info.group(3)
        seq_id_range = (int(info.group(5)), int(info.group(6)))
        sequence_identity = float(info.group(7))

        # Assume a code of 1abcX or 1abcX_N refers to a real PDB structure
        m = re.match('(\d[a-zA-Z0-9]{3})[a-zA-Z](_.*)?$', template_code)
        if m:
            template_db_code = m.group(1).upper()
            l = location.PDBLocation(template_db_code)
        else:
            # Otherwise, look up the PDB file in TEMPLATE PATH remarks
            fname = template_path_map[template_code]
            l = location.InputFileLocation(fname,
                             details="Template for comparative modeling")
        d = dataset.PDBDataset(l)

        # Make the comparative model dataset derive from the template's
        target_dataset.parents.append(d)

        return startmodel.Template(dataset=d, asym_id=template_asym_id,
                                   seq_id_range=seq_id_range,
                                   template_seq_id_range=template_seq_id_range,
                                   sequence_identity=sequence_identity,
                                   alignment_file=alnfile)

    def _parse_pdb_records(self, fh, first_line):
        """Extract information from an official PDB"""
        metadata = []
        details = ''
        for line in fh:
            if line.startswith('TITLE'):
                details += line[10:].rstrip()
            elif line.startswith('HELIX'):
                metadata.append(startmodel.PDBHelix(line))
        return (first_line[50:59].strip(),
                details if details else None, metadata)

    def _parse_details(self, fh):
        """Extract TITLE records from a PDB file"""
        details = ''
        for line in fh:
            if line.startswith('TITLE'):
                details += line[10:].rstrip()
            elif line.startswith('ATOM'):
                break
        return details
