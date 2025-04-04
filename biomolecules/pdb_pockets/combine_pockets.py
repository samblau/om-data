import argparse
import glob
import os
from collections import defaultdict
from typing import Optional

import pandas as pd
from schrodinger.comparison import are_conformers
from schrodinger.structure import Structure, StructureReader
from schrodinger.structutils import measure
from tqdm import tqdm

from biolip_extraction import get_biolip_db


def combine_pockets(biolip_df: pd.DataFrame, input_path: str, output_path: str):
    """
    Combine entries that correspond to the same pocket.

    If a ligand is interacting with multiple receptor chains, they will
    appear as different entries in BioLiP and therefore different structures
    here. We should combine such structures into one structure.

    :param biolip_df: DataFrame storing BioLiP database
    :param path: Path to extracted pockets
    """
    shared_pockets = biolip_df[
        biolip_df.duplicated(
            subset=["pdb_id", "ligand_id", "ligand_chain", "ligand_residue_number"],
            keep=False,
        )
    ]
    grouped_pockets = shared_pockets.groupby(
        ["pdb_id", "ligand_id", "ligand_chain", "ligand_residue_number"],
        group_keys=False,
    )
    file_list = glob.glob(os.path.join(input_path, "*.mae"))

    grouped_file_list = defaultdict(list)
    for f in file_list:
        start = "_".join(os.path.basename(f).split("_")[:2]) + "_"
        if len(os.path.basename(f).split("_")) == 4:
            grouped_file_list[start].append(f)

    for key, item in tqdm(grouped_pockets):
        merge_partners = []
        for idx, row in item.iterrows():
            bs_name = f'{row["receptor_chain"]}{row["binding_site_number_code"]}'
            start = f'{row["pdb_id"]}_{bs_name}_'
            fname = grouped_file_list[start]
            if not fname:
                continue
            merge_partners.append(fname)
        # If we have only one set of merge partners, then there is nothing to merge
        if len(merge_partners) < 2:
            continue

        ## Once we have determined those, we can use the merge_structures logic
        merge_chains = determine_combinable_structures(merge_partners)
        for chain in merge_chains:
            out_name = key[0]
            combined_st = None
            count = 0
            for st in chain:
                combined_st = merge_structures(combined_st, st, count)
                count += 1
                out_name += f"_{'_'.join(os.path.basename(st.title).split('_')[1:-1])}"
            old_names = [st.title for st in chain]
            out_name += f"_{combined_st.formal_charge}"
            for fname in old_names:
                os.remove(fname)
            out_fname = os.path.join(output_path, out_name + ".mae")
            combined_st.title = out_fname
            combined_st.write(out_fname)


def determine_combinable_structures(fname_list):
    ## Determine the sets of the merge partners that can acutally be merged,
    ## for any given ligand/coord protonation state there should only be one from
    ## each binding site that matches. So if we have [(ABS01_state0, ABS02_state1),
    ##          (BBS01_state0, BBS03_state1)]
    ## then there should be only one structure from each that has the same ligand/coord structure
    merge_partners = []
    for fnames in fname_list:
        st_list = []
        for f in fnames:
            st = StructureReader.read(f)
            st.title = f
            st_list.append(st)
        merge_partners.append(st_list)

    merge_chains = defaultdict(list)
    for partner_list in merge_partners:
        for st2 in partner_list:
            for st1 in merge_chains:
                if ligands_are_combinable(st1, st2):
                    merge_chains[st1].append(st2)
                    break
            else:
                merge_chains[st2].append(st2)
    merge_chains = [chain for chain in merge_chains.values() if len(chain) > 1]
    return merge_chains


def ligands_are_combinable(st1, st2):
    lig_extracts = []
    combinable = False
    for st in (st1, st2):
        lig_atoms = st.chain["l"].getAtomList()
        lig = st.extract(lig_atoms)
        lig_extracts.append(lig)
    return are_conformers(*lig_extracts)


def get_ligand_chain_bonds(st: Structure) -> dict[int, tuple]:
    """
    Get bonds between ligand and receptor chains

    We will need to copy these bonds into the combined structure, as we
    have only one copy of the ligand and potentially multiple attachment
    sites.

    :param st: Structure to get bonds in
    :return: Dictionary relating the index of the ligand atom to a tuple
             of the StructureAtom object (so that new numbering can be
             extracted when atoms are deleted) and bond order (which would
             disappear if one of the atoms were deleted)
    """
    comb_cov = {}
    for bond in st.bond:
        chains = {at.chain for at in bond.atom}
        if "l" in chains and any(ch.isupper() for ch in chains):
            if bond.atom1.chain == "l":
                comb_cov[bond.atom1.index] = (bond.atom2, bond.order)
            else:
                comb_cov[bond.atom2.index] = (bond.atom1, bond.order)
    return comb_cov


def merge_structures(
    combined_st: Optional[Structure], st: Structure, count: int
) -> Structure:
    """
    Merge the structures

    This requires:
        1) Determining what ligand-receptor bonds are present in `st`
        2) Deleting duplicated atoms (i.e. the ligand and any coordinating atoms) from `st`
        3) Combining `combined_st` and `st`
        4) Recovering ligand-receptor bonds which may have been deleted in Step 2
        5) If the newly added chain had previously been picked up as
           coordinating atoms, remove those coordinating atoms in favor
           of the equivalent receptor atoms

    :param combined_st: Structure (or None if we are just starting) to
                        collect combined structure into
    :param st: Structure to add to combined_st
    :param count: How many structures have been added (this is to keep track of chain names)
    :return: Newly combined structure
    """
    if combined_st is None:
        return st
    # Get covalent bonds between ligands and receptor chains
    new_cov = get_ligand_chain_bonds(st)

    # Figure out the equivalent atom numbers between
    comb_copy = combined_st.copy()
    comb_copy.extend(st)
    offset = combined_st.atom_total
    remapped_cov = {}
    for pair in measure.get_close_atoms(comb_copy, dist=0.05):
        if max(pair) - offset in new_cov:
            remapped_cov[min(pair)] = new_cov[max(pair) - offset]

    # delete ligand atoms
    ligand_ats = st.chain["l"].getAtomList()
    if "c" in (ch.name for ch in st.chain):
        ligand_ats.extend(st.chain["c"].getAtomList())
    st.deleteAtoms(ligand_ats)
    for at in st.atom:
        at.chain = chr(65 + count)

    # combine structures and recover ligand-receptor bonds
    combined_st.extend(st)
    for lig_at, (chain_at, order) in remapped_cov.items():
        if not combined_st.areBound(lig_at, chain_at.index + offset):
            combined_st.addBond(lig_at, chain_at.index + offset, order)

    # Resolve any coordinating atoms in favor of a receptor atom
    close_atoms = measure.get_close_atoms(combined_st, dist=0.05)
    ats_to_delete = []
    for at1, at2 in close_atoms:
        if combined_st.atom[at2].chain == "c":
            ats_to_delete.append(at2)
        elif combined_st.atom[at1].chain == "c":
            ats_to_delete.append(at1)
    combined_st.deleteAtoms(ats_to_delete)
    return combined_st


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_path", default=".")
    parser.add_argument("--output_path", default=".")
    return parser.parse_args()


def main():
    args = parse_args()
    biolip_df = get_biolip_db(pklpath=args.input_path)
    combine_pockets(biolip_df, args.input_path, args.output_path)


if __name__ == "__main__":
    main()
