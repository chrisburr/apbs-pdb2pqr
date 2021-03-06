#
# pKa calculations with APBS
#
# Copyright University College Dublin & Washington University St. Louis 2004-2007
# All rights reserved
#
__date__="22 April, 2009"
__author__="Jens Erik Nielsen, Todd Dolinsky, Yong Huang, Tommy Carstensen"

debug=False
import os
import sys
import pKaIO_compat
from pKa_base import *
import cPickle
import pMC_mult
import math
import copy
import string

from graph_cut.utils import create_protein_complex_from_matrix, process_desolv_and_background, curve_for_one_group
from graph_cut.titration_curve import get_titration_curves
from graph_cut.create_titration_output import create_output

if debug:
    from Tkinter import *
    from charge_mon import *

    CM=charge_mon()
else:
    CM=None

import shutil

from pka_help import is_sameatom, titrate_one_group
from src.errors import PDB2PKAError

#
# --
#
# from src import pdb
# from src import utilities
# from src import structures
# from src import routines
# from src import protein
# from src import server
#from src.pdb import *
#from src.utilities import *
#from src.structures import *
#from src.definitions import *
#from src.forcefield import *
from src.routines import Routines
#from src.protein import *
#from src.server import *
#from StringIO import *
from src.hydrogens import hydrogenRoutines, hydrogenAmbiguity

#import ligandclean.ligff
from apbs import runAPBS


#
# ----
#
path = os.path.dirname(__file__)
TITRATIONFILE = os.path.join(path,"TITRATION.DAT")

class pKaRoutines:
    """
        Class for running all pKa related functions
    """
    def __init__(self, protein, routines, forcefield, apbs_setup, output_dir, maps = None, sd =None,
                 restart=False, pairene=1.0, test_mode=False):
        """
            Initialize the class using needed objects

            Parameters
                protein:    The PDB2PQR protein object
                routines:   The PDB2PQR routines object
                forcefield: The PDB2PQR forcefield object
        """
        self.protein = protein
        self.routines = routines
        self.forcefield = forcefield
        self.apbs_setup=apbs_setup
        self.pairene = pairene

        self.output_dir=output_dir

        self.APBS=None

        #Output files
        self.output_files = {}
        self.output_files['pka_dat_file_path'] = os.path.join(self.output_dir, 'PKA.DAT')
        self.output_files['desolv_dat_file_path'] = os.path.join(self.output_dir, 'DESOLV.DAT')
        self.output_files['backgr_dat_file_path'] = os.path.join(self.output_dir, 'BACKGR.DAT')
        self.output_files['titcurv_dir'] = os.path.join(self.output_dir, 'TITCURV.DAT')
        self.output_files['matrix_dat_file_path'] = os.path.join(self.output_dir, 'MATRIX.DAT')
        self.output_files['interaction_matrix_dat_file_path'] = os.path.join(self.output_dir, 'INTERACTION_MATRIX.DAT')
        self.output_files['background_interaction_energies_file_path'] = os.path.join(self.output_dir,'background_interaction_energies.txt')
        self.output_files['desolvation_energies_file_path'] = os.path.join(self.output_dir, 'desolvation_energies.txt')

        #
        # Set the phidir - where results of apbscalcs are stored
        #
        if not test_mode:
            self.phidir=os.path.join(self.output_dir,'phidir')
            if not os.path.isdir(self.phidir):
                os.mkdir(self.phidir)

            self.pdb_dumps_dir=os.path.join(self.output_dir,'pdb_dumps')
            if not os.path.isdir(self.pdb_dumps_dir):
                os.mkdir(self.pdb_dumps_dir)

            self.titcurves_dir=os.path.join(self.output_dir,'titration_curves')
            if not os.path.isdir(self.titcurves_dir):
                os.mkdir(self.titcurves_dir)

            self.state_files=os.path.join(self.output_dir,'state_files')

            if restart:
                if os.path.isdir(self.state_files):
                    shutil.rmtree(self.state_files)
                if os.path.isfile(self.state_files):
                    raise ValueError('Target directory is a file! Aborting.')

                for output_file in self.output_files.values():
                    if os.path.isfile(output_file):
                        os.remove(output_file)

            if not os.path.isdir(self.state_files):
                os.mkdir(self.state_files)


        self.pKagroups = self.readTitrationDefinition()

        self.pKas = []

        if not test_mode:
            myHydrogenRoutines = hydrogenRoutines(routines)
            self.hydrogenRoutines = myHydrogenRoutines
        #
        # Not sure this is the best place for the interaction energies...
        #
        self.matrix={}
        self.maps=maps
        self.sd=sd

        #Holding spot for reported warnings.
        self.warnings = []

        #Holding spot for ph values at 0.5 on titration curves.
        self.ph_at_0_5 = {}


    #
    # -----------------------------------------
    #


    def insert_new_titratable_group(self,ligand_titratable_groups):
        """Insert the new titratable groups in to self.pkagroups"""
        group_type=ligand_titratable_groups['type']
        if self.pKagroups.has_key(group_type):
            #
            # Now modify the group so that it will correspond to the group
            # we have in the ligand
            #
            ligand_name='LIG' #Note: we have to implement automatic determination of ligand name
            new_group=copy.deepcopy(self.pKagroups[group_type])
            new_group.DefTitrations[0].modelpKa=ligand_titratable_groups['modelpka']
            new_group.name='LIG'
            new_group.resname='LIG'
            self.pKagroups['LIG']=copy.deepcopy(new_group)
            atom_map=ligand_titratable_groups['matching_atoms']
            #
            # Insert definition into HYDROGEN arrays
            #
            for hdef in self.hydrogenRoutines.hydrodefs:
                if hdef.name==group_type:
                    newdef=copy.deepcopy(hdef)
                    newdef.name=ligand_name

                    #
                    # Change the names in each of the conformatinos
                    #
                    # The name of the H is not changed!
                    #
                    for conformation in newdef.conformations:
                        #
                        # Change the name of the atom that the H is bound to
                        #
                        if atom_map.has_key(conformation.boundatom):
                            conformation.boundatom=atom_map[conformation.boundatom]
                        #
                        # Change the name of the hydrogen
                        #
                        oldhname=conformation.hname
                        conformation.hname='H'+conformation.boundatom
                        #
                        # And then for the individual atom names
                        #
                        for atom in conformation.atoms:
                            if atom_map.has_key(atom.name):
                                atom.name=atom_map[atom.name]
                            elif atom.name==oldhname:
                                atom.name=conformation.hname
                    self.hydrogenRoutines.hydrodefs.append(copy.deepcopy(newdef))
        return

    def dump_protein_file(self, file_name, pdbfile=True):
        lines = self.protein.printAtoms(self.protein.getAtoms(), chainflag=True, pdbfile=pdbfile)
        with open(file_name,'w') as fd:
            self.routines.write( 'dumping protein state to '+ fd.name+'\n')
            for line in lines:
                fd.write(line)

    #
    # -----------------------------------------
    #

    def runpKa(self,ghost=None):
        """
        #    Main driver for running pKa calculations
        """
        self.findTitratableGroups()

        if self.maps==2:
            self.generateMaps()
        #
        # Are we calculating ghost titrations?
        #
        if ghost:
            """ Calculate Pairwise Interactions """
            potentialDifference=self.calculatePotentialDifferences()
            return potentialDifference
        else:
            #
            # Normal pKa calculation
            #
            self.calculateIntrinsicpKa()

            """ Calculate Pairwise Interactions """
            self.calculatePairwiseInteractions()

            """ Calculate Full pKa Value """
            self.calculatepKaValue()
            return

    #
    # -----------------------------------
    #

    def generateMaps(self):
        """
        # generate 3D maps using pdie and sdie
        """
        pKa = self.pKas[0]
        residue = pKa.residue
        pKaGroup = pKa.pKaGroup
        ambiguity = pKa.amb

        self.routines.write("-----> Generating initial coarse grid 3D dielectric and kappa maps\n")
        titration=pKaGroup.DefTitrations[0]
        possiblestates = titration.allstates
        state=possiblestates[0]
        atomnames = self.getAtomsForPotential(pKa,titration)

        self.apbs_setup.set_type('desolv')

        myRoutines = Routines(self.protein, self.routines.verbose)
        myRoutines.updateResidueTypes()
        myRoutines.updateSSbridges()
        myRoutines.updateBonds()
        myRoutines.updateInternalBonds()
        pKa.residue.fixed = 2

        myRoutines.debumpProtein()

        self.zeroAllRadiiCharges()
        self.setCharges(residue, atomnames)
        self.setAllRadii()
        self.getAPBSPotentials(pKa,titration,state)

        self.apbs_setup.maps = 1
        xdiel = 'xdiel_default.dx'
        ydiel = 'ydiel_default.dx'
        zdiel = 'zdiel_default.dx'
        kappa = 'kappa_default.dx'

        if self.sd:
            xdiel, ydiel, zdiel = smooth(xdiel,ydiel,zdiel)

        self.apbs_setup.xdiel = xdiel
        self.apbs_setup.ydiel = ydiel
        self.apbs_setup.zdiel = zdiel
        self.apbs_setup.kappa = kappa
        return


    #
    # -----------------------------------
    #


    def calculatePotentialDifferences(self):
        """
        # calculate potential difference of backbone atoms when each titratable group is set to charged and neutral states
        """
        potentialDifference={}
        for pKa in self.pKas:
            self.get_interaction_energies_setup(pKa,mode='pKD')
            all_potentials=self.all_potentials[pKa].copy()
            residue = pKa.residue
            pKaGroup = pKa.pKaGroup
            ambiguity = pKa.amb

            titgroup='%s:%s:%s' %(residue.chainID, string.zfill(residue.resSeq,4),pKaGroup.name)
            if not potentialDifference.has_key(titgroup):
                potentialDifference[titgroup]={}
                for atom in self.protein.getAtoms():
                    if atom.name in ['N','H','C']:
                        atom_uniqueid=atom.chainID+':'+string.zfill(atom.resSeq,4)+':'+atom.name
                        potentialDifference[titgroup][atom_uniqueid]=0.00

            #
            # Loop over each titration
            #
            for titration in pKaGroup.DefTitrations:
                startstates = titration.startstates
                endstates = titration.endstates
                possiblestates = titration.allstates
                #
                # Loop over each state
                #
                for state in possiblestates:
                    for atom in self.protein.getAtoms():
                        if atom.name in ['N','H','C']:
                            atom_uniqueid=atom.chainID+':'+string.zfill(atom.resSeq,4)+':'+atom.name
                            if state in startstates:
                                potentialDifference[titgroup][atom_uniqueid]-=all_potentials[titration][state][pKa][titration][state][atom_uniqueid]/len(startstates)
                            if state in endstates:
                                potentialDifference[titgroup][atom_uniqueid]+=all_potentials[titration][state][pKa][titration][state][atom_uniqueid]

        return potentialDifference
    #
    # -----------------------------------
    #

    def calculatePairwiseInteractions(self):
        """
        # Calculate the pairwise interaction energies
        """
        for pKa in self.pKas:
            self.get_interaction_energies_setup(pKa)
        return

    #
    # ------------------
    #

    def get_default_protonation_states(self, residues):
        """Get default protonation states for a list of residues"""
        defaultprotonationstates = {}
        for residue in residues:
            for atom in residue.atoms:
                self.routines.write(str(atom)+'\n')
            key = residue.name + '_' + residue.chainID + '_' + str(residue.resSeq)
            if residue.name in ["ASP", "GLU"]:
                defaultprotonationstates[key] = "0"
            elif residue.name in ["LYS", "TYR"]:
                defaultprotonationstates[key] = "1"
            elif residue.name == "ARG":
                defaultprotonationstates[key] = "1+2+3+4+5"
            elif residue.name == "HIS":
                if residue.hasAtom("HD1") and residue.hasAtom("HE2"):
                    defaultprotonationstates[key] = "1+2"
                elif residue.hasAtom("HD1"):
                    defaultprotonationstates[key] = "1"
                elif residue.hasAtom("HE2"):
                    defaultprotonationstates[key] = "2"
            if residue.isNterm:
                key = 'NTR' + '_' + residue.chainID + '_' + str(residue.resSeq)
                defaultprotonationstates[key] = "1+2"
            elif residue.isCterm:
                key = 'CTR' + '_' + residue.chainID + '_' + str(residue.resSeq)
                defaultprotonationstates[key] = "0"

        return defaultprotonationstates

    #
    # -----
    #

    def get_interaction_energies_setup(self,pKa,mode='pkacalc'):
        """Perform the setup for the interaction energy calculation"""
        residue = pKa.residue
        pKaGroup = pKa.pKaGroup
        ambiguity = pKa.amb

        #
        # Loop over each titration
        #
        self.all_potentials={}
        if not self.matrix.has_key(pKa):
            self.matrix[pKa]={}
            self.all_potentials[pKa]={}
        #
        for titration in pKaGroup.DefTitrations:
            if not self.matrix[pKa].has_key(titration):
                self.matrix[pKa][titration]={}
                self.all_potentials[pKa][titration]={}
            #
            # Get the atomnames
            #
            atomnames = self.getAtomsForPotential(pKa,titration)
            atomlist=[]
            for atomname in atomnames:
                atomlist.append(residue.getAtom(atomname))
            center=self.get_atoms_center(atomlist)
            self.apbs_setup.setfineCenter(center)
            #
            # Get all states
            #
            possiblestates = titration.allstates
            for state in possiblestates:
                #
                # do not allow other states for this residue to be explored
                #
                for other_state in possiblestates:
                    residue.stateboolean[self.get_state_name(titration.name, other_state)] = False
                #
                # Here we switch the center group to a particular state
                #
                self.hydrogenRoutines.switchstate('pKa', ambiguity, self.get_state_name(titration.name,state))
                name='%s_%s_%s_%s' %(titration.name,
                                     pKa.residue.chainID,
                                     pKa.residue.resSeq,
                                     self.get_state_name(titration.name,state))


                intenename = os.path.join(self.state_files, name+'.interaction_energy.pickle')
                allpotsname = os.path.join(self.state_files, name+'.interaction_energy_allpots.pickle')
                residue.stateboolean[self.get_state_name(titration.name, state)] = True
                if not os.path.isfile(intenename):
                    pdb_file = os.path.join(self.pdb_dumps_dir, name+'_interaction_setup_input.pdb')


                    self.hbondOptimization()
                    self.dump_protein_file(pdb_file)
                    self.zeroAllRadiiCharges()
                    self.setAllRadii()
                    self.setCharges(residue, atomnames)
                #
                # get_interaction_energies get the potential at all titratable groups due the charges
                # this group
                #
                self.matrix[pKa][titration][state],self.all_potentials[pKa][titration][state]=self.get_interaction_energies(pKa,titration,state,mode, intenename, allpotsname)

        return

    #
    # ----
    #

    def get_interaction_energies(self,pKa_center,titration_center,state_center,mode,intene_file_name,allpots_file_name):
        """Get the potentials and charges at all titratable groups"""
        self.routines.write('------------>Charge - charge interactions for group: %s, state: %s\n' %
                            (pKa_center.residue.resSeq,self.get_state_name(titration_center.name, state_center)))

        read_allpots=None
        #
        if os.path.isfile(intene_file_name):
            with open(intene_file_name) as fd:
                savedict=cPickle.load(fd)
            #
            # pKD?
            #
            if mode=='pKD' and os.path.isfile(allpots_file_name):
                try:
                    sys.stdout.flush()
                    with open(allpots_file_name) as fd:
                        allsavedict=cPickle.load(fd)
                    read_allpots=1
                except EOFError:
                    self.routines.write('\n')
                    self.routines.write('File %s is corrupt.\nDeleting and continuing...\n' %allpots_file_name)
                    os.unlink(allpots_file_name)
                    allsavedict={}
            else:
                allsavedict={}
        else:
            self.routines.write('Not found '+intene_file_name+'\n')
            savedict={}
            allsavedict={}
        #
        # Run APBS and get the interaction with all other states
        #
        #if debug:
        #    CM.set_calc('IE %s %s' %(pKa_center.residue.resSeq,state_center))
        if savedict=={} or (allsavedict=={} and mode=='pKD'):
            self.apbs_setup.set_type('intene')
            potentials=self.getAPBSPotentials(pKa_center,titration_center,state_center,cleanup=False)

        #
        # construct this side
        #
        energies={}
        all_potentials={}
        #
        # Loop over all groups
        #
        calculated_energy=False
        for pKa in self.pKas:
            residue = pKa.residue
            pKaGroup = pKa.pKaGroup
            ambiguity = pKa.amb
            #
            # Loop over each titration
            #
            if not energies.has_key(pKa):
                energies[pKa]={}
                all_potentials[pKa]={}
            #
            for titration in pKaGroup.DefTitrations:
                if not energies[pKa].has_key(titration):
                    energies[pKa][titration]={}
                    all_potentials[pKa][titration]={}
                #
                # Get all states
                #
                possiblestates = titration.allstates
                atomnames=self.getAtomsForPotential(pKa,titration)
                #
                # Calculate the interaction energy with a charged state. If that energy is not large, then
                # assume that all other energies for this titgroup are zero
                #
                start_state=[]
                for state1 in possiblestates:
                    if self.is_charged(pKa,titration,state1):
                        start_state=[state1]
                #
                # Loop over all states for this residue
                #

                for state in start_state+possiblestates:
                    all_potentials[pKa][titration][state]={}
                    name='%s_%s_%s_%s' %(titration.name,pKa.residue.chainID,pKa.residue.resSeq,self.get_state_name(titration.name,state))
                    #
                    # Check if we have values for this calculation already
                    #
                    if savedict.has_key(name):
                        energies[pKa][titration][state]= savedict[name]
                        if mode=='pKD':
                            if allsavedict.has_key(name):
                                all_potentials[pKa][titration][state]=allsavedict[name]
                        continue
                    #
                    # Calculate the energy
                    #
                    calculated_energy=True
                    #
                    # Allow optimization of states for all other groups
                    #
                    for other_pKa in self.pKas:
                        if pKa==other_pKa:
                            continue
                        for other_titration in other_pKa.pKaGroup.DefTitrations:
                            #
                            # Don't change stateboolean for the center group
                            #
                            if other_titration==titration_center:
                                continue
                            #
                            # Allow all states for all other residues (this matters only for the bump score calculation)
                            #
                            other_possiblestates = other_titration.allstates
                            for other_state in other_possiblestates:
                                other_pKa.residue.stateboolean[self.get_state_name(other_titration.name, other_state)] = True
                    #
                    # Switch to the particular state we want to measure for
                    #
                    self.hydrogenRoutines.switchstate('pKa', ambiguity, self.get_state_name(titration.name,state))
                    self.routines.write(str(titration)+'\n')
                    self.routines.write(titration.name+'\n')
                    for other2_state in titration.allstates:
                        pKa.residue.stateboolean[self.get_state_name(titration.name,other2_state)]=False
                    pKa.residue.stateboolean[self.get_state_name(titration.name,other2_state)]=True
                    #
                    # We have to do a full Hbond optimization here to get the correct bumpscore
                    #
                    bump=False
                    pdb_file = os.path.join(self.pdb_dumps_dir, name+'_interaction_input.pdb')


                    self.hbondOptimization() # Optimize the hydrogens to actually put the hydrogen in the right position
                    self.dump_protein_file(pdb_file)

                    if self.routines.getbumpscore(pKa_center.residue)>100:
                        bump=True
                    elif self.routines.getbumpscore(pKa.residue)>100:
                        bump=True
                    #
                    #
                    #
                    potentials=self.getmoreAPBSPotentials()
                    self.zeroAllRadiiCharges()
                    self.setAllRadii()
                    self.setCharges(residue, atomnames)
                    #
                    # Get atoms for potential
                    #
                    atomlist=[]
                    for atomname in atomnames:
                        if residue.getAtom(atomname) not in atomlist:
                            atomlist.append(residue.getAtom(atomname))

                    energy=0.0
                    count=0
                    for atom in self.protein.getAtoms():
                        for atom2 in atomlist:
                            if is_sameatom(atom,atom2):
                                energy=energy+potentials[count]*atom.get("ffcharge")
                        count=count+1

                    #
                    # We set all energies with self to zero
                    #
                    if pKa==pKa_center:
                        energies[pKa][titration][state]=0.0
                    else:
                        if bump:
                            energies[pKa][titration][state]=100000.0 # exclude this combination
                        else:
                            energies[pKa][titration][state]=energy
                    #
                    # Check if this is the charged state
                    #
                    if state==start_state[0]:
                        self.routines.write('\nENERGY; %f\n\n' %energy)
                        #raw_input('continue?')
                        if abs(energy)<self.pairene and mode!='pKD':
                            #
                            # If energy is below cutoff then do not explore other states
                            #
                            for stateset in possiblestates:
                                if stateset==start_state[0]:
                                    continue
                                energies[pKa][titration][stateset]=0.0
                                name2='%s_%s_%s_%s' %(titration.name,pKa.residue.chainID,pKa.residue.resSeq,self.get_state_name(titration.name,stateset))
                                savedict[name2]=energies[pKa][titration][stateset]
                                self.routines.write('\n\n=======SKIPPING NEUTRAL STATES==============\n\n\n')
                    #
                    # Save in dict
                    #
                    savedict[name]=energies[pKa][titration][state]
                    #
                    # If running in pKD mode then we also return all potentials
                    #
                    if mode=='pKD':
                        count=0
                        for atom in self.protein.getAtoms():
                            atom_uniqueid=atom.chainID+':'+string.zfill(atom.resSeq,4)+':'+atom.name
                            all_potentials[pKa][titration][state][atom_uniqueid]=potentials[count]
                            count=count+1
                        allsavedict[name]=all_potentials[pKa][titration][state]
        #
        # Get rid of APBS instance
        #
        if self.APBS is not None:
            self.APBS.cleanup()
            self.APBS=None
        #
        # Dump a pickle file
        #
        if calculated_energy:
            with open(intene_file_name,'w') as fd:
                cPickle.dump(savedict,fd)
            if mode=='pKD' and not read_allpots:
                with open(allpots_file_name,'w') as fd:
                    cPickle.dump(allsavedict,fd)
        return energies,all_potentials

    #
    # ----------------------------------
    #

    def calculatepKaValue(self):
        """
        Calculate the pKa Value

        We use a graph cut method because it gives the right answer.
        """
        #
        # First we need to correct all the interaction energies to make sure that terms aren't
        # counted twice
        #
        correct_matrix=self.correct_matrix()
        
        #
        #check that the energies in the interaction matrix are no larger than 80kT; if so, terminate the program.
        #
        for pKa in self.pKas:
            for titration in pKa.pKaGroup.DefTitrations:
                for pKa2 in self.pKas:
                    for titration2 in pKa2.pKaGroup.DefTitrations:
                        pos_states = titration.allstates
                        pos_states.sort()
                        for state1 in pos_states:
                            states2 = titration2.allstates
                            states2.sort()
                            for state2 in states2:
                                linen = correct_matrix[pKa][titration][state1][pKa2][titration2][state2]
                                if abs(linen) > 80:
                                    exitString = "\nError!!: Detected abnormally large interaction energy %5.4f kT before calculating pKas\n" % linen
                                    exitString = exitString + "Terminating Program.\n"
                                    sys.exit(exitString)
                            

        protein_complex = create_protein_complex_from_matrix(correct_matrix)
        for pKa in self.pKas:
            process_desolv_and_background(protein_complex, pKa)

        protein_complex.simplify()

        curves = get_titration_curves(protein_complex)

        create_output(self.titcurves_dir, curves)

        pka_values, pH_values = self.find_pka_and_pH(curves)
        print pka_values
        print pH_values
        self.ph_at_0_5 = pH_values

        ln10=math.log(10)


        self.routines.write('\n\nFinal pKa values\n\n')
        pkas={}
        for pKa in self.pKas:
            pKaGroup = pKa.pKaGroup
            Gtype=pKa.pKaGroup.type
            for titration in pKaGroup.DefTitrations:
                name=pKa.uniqueid
                pKa_value = pka_values[pKaGroup.name, pKa.residue.chainID, str(pKa.residue.resSeq)]
                pkas[name]={'pKa':pKa_value}
                self.routines.write("name: %s, PKAS[name]: %s\n" % (name, pkas[name]))
                pkas[name]['modelpK']=titration.modelpKa
                #
                # Find an uncharged reference state
                #
                ref_state=self.neutral_ref_state[pKa][titration]
                #
                all_states=titration.allstates
                all_states.sort()
                for state in all_states:
                    if self.is_charged(pKa,titration,state)==1:
                        dpKa_desolv=(pKa.desolvation[self.get_state_name(titration.name,state)]-pKa.desolvation[self.get_state_name(titration.name,ref_state)])/ln10
                        dpKa_backgr=(pKa.background[self.get_state_name(titration.name,state)]-pKa.background[self.get_state_name(titration.name,ref_state)])/ln10
                        #
                        # Make acid and base modifications
                        #
                        if Gtype=='base':
                            dpKa_desolv=-dpKa_desolv
                            dpKa_backgr=-dpKa_backgr

                pkas[name]['desolv']=dpKa_desolv
                pkas[name]['backgr']=dpKa_backgr

                self.routines.write('Desolvation '+ str(pKa.desolvation) +'\n')
                self.routines.write('Background '+ str(pKa.background) +'\n')

                possiblestates = titration.allstates
                #
                # Record the number of states for each titratable group
                #
                #state_counter.append(len(possiblestates))

                pos_states=possiblestates
                chg_intpkas=[]
                neut_intpkas=[]
                pos_statenames=[]
                pos_states.sort()
                for state in pos_states:
                    pos_statenames.append(self.get_state_name(titration.name,state))
                self.routines.write('States'+ str(pos_statenames) +'\n')
                for state in pos_states:
                    crg=self.is_charged(pKa,titration,state)
                    if abs(crg)>0.1:
                        chg_intpkas.append(pKa.intrinsic_pKa[state])
                    else:
                        neut_intpkas.append(pKa.intrinsic_pKa[state])
                    self.routines.write('State: %6s, charge: %5.2f, intpka: %5.3f\n' %(self.get_state_name(titration.name,state),crg,pKa.intrinsic_pKa[state]))

                pkas[name]['intpka']=pKa.simulated_intrinsic_pKa
                pkas[name]['delec']=pKa_value-pkas[name]['intpka']
                #
                # Print
                #
                pKa.pKa=pKa_value
                self.routines.write('Simulated intrinsic pKa: %5.3f, delec: %5.3f\n' %(pkas[name]['intpka'],pkas[name]['delec']))
                self.routines.write('%s final pKa: %5.2f\n' %(pKa.uniqueid,pKa.pKa))
                self.routines.write('=============================================\n\n')

        # Write a WHAT IF -style pKa file

        X=pKaIO_compat.pKaIO()

        X.write_pka(self.output_files['pka_dat_file_path'],pkas,format='pdb2pka')
        #
        # Desolv file % Backgr file
        #
        X.desolv={}
        X.backgr={}
        for name in pkas.keys():
            X.desolv[name]=pkas[name]['desolv']
            X.backgr[name]=pkas[name]['backgr']


        X.write_desolv(self.output_files['desolv_dat_file_path'],format='pdb2pka')
        X.write_backgr(self.output_files['backgr_dat_file_path'],format='pdb2pka')

        #
        # Write the charge matrix
        #
        X.write_pdb2pka_matrix(self.output_files['matrix_dat_file_path'], correct_matrix)
        return

    #
    # ----
    #


    def find_pka_and_pH(self, curves):
        """
        Detect and report non Henderson-Hasselbalch behavior in titration curves.
        Find the pKa value for each curve.
        Also find ph when 0.5 (-0.5 for acid) is crossed on the curve.
        """
        pKa_results = {}
        pH_results = {}

        adjacent_data_points = 5

        for name, curve in curves.iteritems():
            bad_curve = False
            curve_calc_point = 0.5

            pKa_value = -999.0

            #List of booleans of which side of curve_calc_point charges fell on.
            charge_side = [x[1] > curve_calc_point for x in curve]

            #Check to see if we never cross 0.5 or -0.5 at all
            if all(charge_side) or not any(charge_side):
                warning = "WARNING: UNABLE TO CACLCULATE PKA FOR {name}\n".format(name=name)
                print warning,
                self.warnings.append(warning)
                pKa_results[name] = pKa_value
                continue


            #Find all unique adjacent pairs of False and True
            side_pairs = set(zip(charge_side[:-1], charge_side[1:]))

            if (True,False) not in side_pairs:
                warning =  "WARNING: {name} DOES NOT EXHIBIT Henderson-Hasselbalch BEHAVIOR\n".format(name=name)
                print warning,
                self.warnings.append(warning)

                warning = "WARNING: {name} TITRATION CURVE IS BACKWARDS\n".format(name=name, calc=curve_calc_point)
                print warning,
                self.warnings.append(warning)
                cross_index = charge_side.index(True)
                bad_curve = True
            else:
                cross_index = charge_side.index(False)

            #We should always see (True, False) in perfect Henderson-Hasselbalch behavior
            #(False, True) means we've crossed back over the line and therefore our PKA value is in question.
            if (True,False) in side_pairs and (False,True) in side_pairs:
                warning =  "WARNING: {name} DOES NOT EXHIBIT Henderson-Hasselbalch BEHAVIOR\n".format(name=name)
                print warning,
                self.warnings.append(warning)
                warning = "WARNING: {name} TITRATION CURVE CROSSES {calc} AT LEAST TWICE\n".format(name=name, calc=curve_calc_point)
                print warning,
                self.warnings.append(warning)

                bad_curve = True

            prevous_cross_index = cross_index - 1

            #linear interpolation
            charge0, ph0 =  curve[prevous_cross_index]
            charge1, ph1 =  curve[cross_index]

            try:
                ph_at_0_5 = ph0 + ((ph1-ph0) * ((curve_calc_point-charge0)/(charge1-charge0)))
                pH_results[name] = ph_at_0_5
            except ZeroDivisionError:
                warning = "WARNING: UNABLE TO CACLCULATE pH FOR {name}, Divide by zero.\n".format(name=name)
                print warning,
                self.warnings.append(warning)

            if not bad_curve:
                print "{name} exhibits Henderson-Hasselbalch behavior.".format(name=name)

            #Calc pKa value
            start = max(0, prevous_cross_index-adjacent_data_points)
            end = cross_index + adjacent_data_points
            pka_pairs = curve[start:end]

            try:
                pkas = [pH-math.log10(abs(v)/(1.0-abs(v))) for pH, v in pka_pairs]
                pKa_value = sum(pkas)/float(len(pkas))
            except ZeroDivisionError:
                warning = "WARNING: UNABLE TO CACLCULATE PKA FOR {name}, Divide by zero.\n".format(name=name)
                print warning,
                self.warnings.append(warning)


            pKa_results[name] = pKa_value



        return pKa_results, pH_results

    def correct_matrix(self):
        """Correct the matrix so that all energies are correct, i.e.
        that we subtract the neutral-charged and charged-neutral interaction energies
        in the right places.
        After having fixed this we make the matrix symmetric"""
        corrected_matrix={}
        #
        # Loop over the original matrix
        #
        for pKa1 in self.pKas:
            corrected_matrix[pKa1]={}
            pKaGroup = pKa1.pKaGroup
            for titration1 in pKaGroup.DefTitrations:
                corrected_matrix[pKa1][titration1]={}
                #
                # Get the reference state for first state
                #
                ref_state1=self.neutral_ref_state[pKa1][titration1]

                possible_states = titration1.allstates
                possible_states.sort()
                for state1 in possible_states:
                    corrected_matrix[pKa1][titration1][state1]={}
                    for pKa2 in self.pKas:
                        corrected_matrix[pKa1][titration1][state1][pKa2]={}
                        pKaGroup2 = pKa2.pKaGroup
                        for titration2 in pKaGroup2.DefTitrations:
                            corrected_matrix[pKa1][titration1][state1][pKa2][titration2]={}
                            #
                            # Get the reference state for the second state
                            #
                            ref_state2=self.neutral_ref_state[pKa2][titration2]
                            states2=titration2.allstates
                            states2.sort()
                            for state2 in states2:
                                #
                                # Now figure out what the correct energy for the interaction is
                                #
                                if state1==ref_state1 and state2==ref_state2:
                                    # No interaction for ref-ref
                                    value=0.0
                                else:
                                    value=self.matrix[pKa1][titration1][state1][pKa2][titration2][state2]
                                    #
                                    # Subtract Eint(state1,ref2)+Eint(state2,ref1) and add Eint(ref1,ref2)
                                    #
                                    state1_ref2=self.matrix[pKa1][titration1][state1][pKa2][titration2][ref_state2]
                                    state2_ref1=self.matrix[pKa1][titration1][ref_state1][pKa2][titration2][state2]
                                    ref1_ref2=self.matrix[pKa1][titration1][ref_state1][pKa2][titration2][ref_state2]
                                    #
                                    # If this is a bump state, then disallow it.
                                    # If we have a bump for a reference state, then the code below will
                                    # give a slightly wrong value.... This should be fixed.
                                    #
                                    if abs(value)>1000.0:
                                        value=100000.0
                                    elif abs(state1_ref2)>1000.0 or abs(state2_ref1)>1000.0 or abs(ref1_ref2)>1000.0:
                                        value=value
                                    else:
                                        value=value-state1_ref2-state2_ref1+ref1_ref2
                                #
                                # Insert this value in the corrected matrix
                                #
                                self.routines.write( ' '.join((str(pKa1.uniqueid),
                                                               titration1.name,
                                                               state1,
                                                               str(pKa2.uniqueid),
                                                               titration2.name,
                                                               state2,
                                                               str(value)))+'\n')
                                corrected_matrix[pKa1][titration1][state1][pKa2][titration2][state2]=value
        #
        # Make matrix symmetric
        #
        with open(self.output_files['interaction_matrix_dat_file_path'], 'w') as outfile:
            outfile.write('Interaction energy matrix\n')
            outfile.write('%25s %25s %10s %10s %16s\n' %('Group1','Group 2','State G1','State G2','Interaction energy (kT)'))
            symmetric_matrix={}
            for pKa1 in self.pKas:
                symmetric_matrix[pKa1]={}
                pKaGroup = pKa1.pKaGroup
                for titration1 in pKaGroup.DefTitrations:
                    symmetric_matrix[pKa1][titration1]={}
                    possible_states = titration1.allstates
                    possible_states.sort()
                    for state1 in possible_states:
                        symmetric_matrix[pKa1][titration1][state1]={}
                        for pKa2 in self.pKas:
                            symmetric_matrix[pKa1][titration1][state1][pKa2]={}
                            pKaGroup2 = pKa2.pKaGroup
                            for titration2 in pKaGroup2.DefTitrations:
                                symmetric_matrix[pKa1][titration1][state1][pKa2][titration2]={}
                                #
                                # Get the reference state for the second state
                                #
                                states2=titration2.allstates
                                states2.sort()
                                for state2 in states2:
                                    #
                                    # Now figure out what the correct energy for the interaction is
                                    #
                                    value1=corrected_matrix[pKa1][titration1][state1][pKa2][titration2][state2]
                                    value2=corrected_matrix[pKa2][titration2][state2][pKa1][titration1][state1]
                                    #
                                    # Insert the average value in the symetric matrix
                                    #
                                    average = (value1+value2)/2.0
                                    outfile.write('%25s %25s %10s %10s %6.3f %6.3f %6.3f\n' %(pKa1.uniqueid,pKa2.uniqueid,state1,state2,value1,value2,average))

                                    symmetric_matrix[pKa1][titration1][state1][pKa2][titration2][state2]=average
        return symmetric_matrix

    #
    # ----------------------------------
    #

    def is_charged(self,pKa,titration,state):
        """
        # Check if this state is a charged state
        """
        #
        # Check if there are any other titratable groups in this residue
        #
        for other_pKa in self.pKas:
            if pKa==other_pKa:
                continue
            for other_titration in other_pKa.pKaGroup.DefTitrations:
                self.hydrogenRoutines.switchstate('pKa',other_pKa.amb,self.get_state_name(other_titration.name,self.neutral_ref_state[other_pKa][other_titration]))
        #
        # Get charge
        #
        ambiguity = pKa.amb
        self.hydrogenRoutines.switchstate('pKa', ambiguity, self.get_state_name(titration.name,state))
        residue = pKa.residue
        pKaGroup = pKa.pKaGroup
        sum=0.0
        for atom in residue.getAtoms():
            atomname = atom.get("name")
            if atomname.find('FLIP')!=-1:
                continue
            charge, radius = self.forcefield.getParams1(residue, atomname)
            sum=sum+charge

        return abs(sum)>0.05

    #
    # ----------------------------------
    #

    def calculateIntrinsicpKa(self):
        """
        #  Calculate the intrinsic pKa values for all titratable groups
        """
        self.calculateDesolvation()
        self.calculateBackground()
        #
        # Calculate the intrinsic pKas
        #
        # Print what we got
        #
        for pKa in self.pKas:
            self.routines.write("======== Residue: %s ========\n" % (pKa.residue))
            self.routines.write('     State\tModel pKa\tDesolvation\tBackground\n')
            for titration in pKa.pKaGroup.DefTitrations:
                for state in titration.allstates:
                    self.routines.write( state+'\n')
                    self.routines.write( self.get_state_name(titration.name,state)+'\n')
                    self.routines.write( str(pKa.desolvation[self.get_state_name(titration.name,state)])+'\n')
                    self.routines.write( str(pKa.background[self.get_state_name(titration.name,state)])+'\n')
                    self.routines.write('%10s\t%5.3f\t\t%5.3f\t\t%5.3f\n' %(self.get_state_name(titration.name,state),
                                                            titration.modelpKa,
                                                            pKa.desolvation[self.get_state_name(titration.name,state)],
                                                            pKa.background[self.get_state_name(titration.name,state)]))
        self.routines.write('\n\n')
        #
        # We calculate an intrinsic pKa for every possible <startstate> -> <endstate> transition
        #
        ln10=math.log(10)
        for pKa in self.pKas:
            pKaGroup=pKa.pKaGroup
            Gtype=pKa.pKaGroup.type
            #
            # We measure intrinsic pKa values against a single reference state
            #
            for titration in pKaGroup.DefTitrations:
                #
                # Find the uncharged reference state
                #
                ref_state=self.neutral_ref_state[pKa][titration]
                #
                #
                #
                all_states=titration.allstates
                all_states.sort()
                for state in all_states:
                    if self.is_charged(pKa,titration,state)==1:
                        dpKa_desolv=(pKa.desolvation[self.get_state_name(titration.name,state)]-
                                     pKa.desolvation[self.get_state_name(titration.name,ref_state)])/ln10
                        dpKa_backgr=(pKa.background[self.get_state_name(titration.name,state)]-
                                     pKa.background[self.get_state_name(titration.name,ref_state)])/ln10
                        #
                        # Make acid and base modifications
                        #
                        if Gtype=='base':
                            dpKa_desolv=-dpKa_desolv
                            dpKa_backgr=-dpKa_backgr
                        #
                        # Now calculate intrinsic pKa
                        #
                        intpKa=titration.modelpKa+dpKa_desolv+dpKa_backgr
                        self.routines.write('Energy difference for %6s   -> %6s [reference state] is %5.2f pKa units\n' %(self.get_state_name(titration.name,state),
                                                                                                          self.get_state_name(titration.name,ref_state),
                                                                                                          intpKa))
                        pKa.intrinsic_pKa[state]=intpKa
                    else:
                        #
                        # Neutral states
                        #
                        dpKa_desolv=(pKa.desolvation[self.get_state_name(titration.name,state)]-
                                     pKa.desolvation[self.get_state_name(titration.name,ref_state)])/ln10
                        dpKa_backgr=(pKa.background[self.get_state_name(titration.name,state)]-
                                     pKa.background[self.get_state_name(titration.name,ref_state)])/ln10
                        #
                        # Make acid and base modifications
                        #
                        if Gtype=='base':
                            dpKa_desolv=-dpKa_desolv
                            dpKa_backgr=-dpKa_backgr
                        dpKa=dpKa_desolv+dpKa_backgr
                        self.routines.write('Energy difference for %6s   -> %6s [reference state] is %5.2f pKa units\n' %(self.get_state_name(titration.name,state),
                                                                                                          self.get_state_name(titration.name,ref_state),
                                                                                                          dpKa))
                        pKa.intrinsic_pKa[state]=dpKa
            # -----------------------------------------------------------------
            # Get the intrinsic pKa with a small MC calculation
            #
#             acidbase=[]
#             is_charged=[]
#             intpKas=[]
#             for titration in pKaGroup.DefTitrations:
#                 #
#                 # Acid/Base
#                 #
#                 if pKaGroup.type=='acid':
#                     acidbase.append(-1)
#                 else:
#                     acidbase.append(1)
#                 #
#                 #
#                 #
#                 possiblestates = titration.allstates
#                 #
#                 # Record the number of states for each titratable group
#                 #
#                 pos_states=possiblestates
#                 pos_states.sort()
#                 for state in pos_states:
#                     #
#                     # Is this a charged state?
#                     #
#                     crg=self.is_charged(pKa,titration,state)
#                     is_charged.append(crg)
#                     intpKas.append(pKa.intrinsic_pKa[state])
#             intpka=titrate_one_group(name='%s' %(pKa.residue),intpkas=intpKas,is_charged=is_charged,acidbase=acidbase)
            curve = curve_for_one_group(pKa)
            pka_values, _ = self.find_pka_and_pH(curve)
            intpka = pka_values.values()[0]
            pKa.simulated_intrinsic_pKa=intpka
        return

    #
    # --------------------------------
    #

    def hbondOptimization(self):
        """
        #
        #   Routines needed for H-bond optimization
        #
        """
        # Setting up
        myRoutines = Routines(self.protein, self.routines.verbose)
        myRoutines.updateResidueTypes()

        myRoutines.updateBonds()
        #myRoutines.updateInternalBonds()
        myRoutines.updateSSbridges()

        myRoutines.debumpProtein()

        # Initialize H-bond optimization
        self.hydrogenRoutines.setOptimizeableHydrogens()
        self.hydrogenRoutines.initializeFullOptimization()

        # Full optimization
        self.hydrogenRoutines.optimizeHydrogens()

        # Clean up, debump
        self.hydrogenRoutines.cleanup()
        myRoutines.setStates() # this identifies the protonation states to pdb2pqr
        #myRoutines.debumpProtein() # why do we debump after setting the states?

        return

    #
    # --------------------
    #

    def calculateBackground(self,onlypKa=None):
        """
        #    Calculate background interaction energies
        """

        backgroundname = os.path.join(self.state_files,'background_interaction_energies.pickle')
        if os.path.isfile(backgroundname):
            with open(backgroundname) as fd:
                savedict=cPickle.load(fd)
        else:
            savedict={}

        for pKa in self.pKas:
            if onlypKa:
                if not pKa==onlypKa:
                    continue
            residue = pKa.residue
            pKaGroup = pKa.pKaGroup
            ambiguity = pKa.amb

            self.routines.write("-----> Finding Background Interaction Energy for %s %s\n" %(residue.name, residue.resSeq))
            #
            # Loop over all titrations in this group
            #
            for titration in pKaGroup.DefTitrations:
                #
                # Get all the states for this titration
                #
                possiblestates = titration.startstates + titration.endstates
                #
                # Loop over all states and calculate the Background Interaction energy for each
                #
                for state in possiblestates:
                    #
                    # Set the name for this energy
                    #
                    name='%s_%s_%s_%s' %(titration.name,pKa.residue.chainID,pKa.residue.resSeq,self.get_state_name(titration.name,state))
                    if savedict.has_key(name):
                        pKa.background[self.get_state_name(titration.name,state)] = savedict[name]
                        continue
                    #
                    # Do not allow any other states to be explored
                    #
                    for state2 in possiblestates:
                        residue.stateboolean[self.get_state_name(titration.name,state2)]=False
                    #
                    # This is the state we are calculating for
                    #
                    residue.stateboolean[self.get_state_name(titration.name, state)] = True
                    #
                    # Get the atoms where we will measure the potential
                    #
                    firststate = possiblestates[0]
                    atomnames = self.getAtomsForPotential(pKa,titration)
                    atomlist=[]
                    for atomname in atomnames:
                        atomlist.append(residue.getAtom(atomname))
                    #
                    # Switch the states of all other titratable groups to the neutral reference state
                    #
                    for other_pKa in self.pKas:
                        if pKa==other_pKa:
                            continue
                        for other_titration in other_pKa.pKaGroup.DefTitrations:
                            #
                            # For each residue we first set all states to False in stateboolean
                            # This means that they cannot be explored during a pKa calculation
                            # Afterwards we set stateboolean to True for the neutral ref state
                            #
                            other_possiblestates = other_titration.allstates
                            for other_state in other_possiblestates:
                                if not self.is_charged(other_pKa,other_titration,other_state):
                                    other_pKa.residue.stateboolean[self.get_state_name(other_titration.name, other_state)] = True
                                else:
                                    other_pKa.residue.stateboolean[self.get_state_name(other_titration.name, other_state)] = False
                            #
                            #self.hydrogenRoutines.switchstate('pKa',other_pKa.amb,
                            #                                  self.get_state_name(other_titration.name,
                            #                                                      self.neutral_ref_state[other_pKa][other_titration]))
                            other_pKa.residue.stateboolean[self.neutral_ref_state[other_pKa][other_titration]]=True
                    #
                    # Switch the state for the group in question
                    #
                    self.routines.write("----------> Calculating Background for state %s\n" % (self.get_state_name(titration.name,state)))
                    self.hydrogenRoutines.switchstate('pKa', ambiguity, self.get_state_name(titration.name,state))

                    # Not allowing current protonation state to be explored during H-bond optimization
                    #residue.stateboolean[self.get_state_name(titration.name, state)] = False

                    pdb_file_name = os.path.join(self.pdb_dumps_dir, name+'_background_input.pdb')
                    self.dump_protein_file(pdb_file_name)

                    self.hbondOptimization()

                    # residue.stateboolean returns to default value (True)
                    #residue.stateboolean[self.get_state_name(titration.name, state)] = True

                    self.zeroAllRadiiCharges()
                    self.setAllRadii()

                    pqr_file_name = os.path.join(self.pdb_dumps_dir, name+'_background_input.pqr')
                    self.dump_protein_file(pqr_file_name, pdbfile=False)

                    #
                    # Set charges on all other residues
                    #
                    for chain in self.protein.getChains():
                        for otherresidue in chain.get("residues"):
                            if residue == otherresidue:
                                continue
                            otherlist = []
                            for atom in otherresidue.atoms:
                                if not atom:
                                    continue
                                if atom.get('name').find('FLIP')==-1:
                                    otherlist.append(atom.get("name"))
                            self.setCharges(otherresidue, otherlist)

                    #
                    # Center the map on our residue
                    #
                    center=self.get_atoms_center(atomlist)

                    all_center,extent=self.apbs_setup.getCenter()

                    #
                    # For small proteins we set the center to the center of the molecule
                    #
                    #if extent[0]<20.0 or extent[1]<20.0 or extent[2]<20.0:
                    #    self.apbs_setup.setfineCenter(all_center)
                    #else:
                    self.apbs_setup.setfineCenter(center)
                    self.apbs_setup.set_type('background')
                    #
                    # Run APBS
                    #
                    if debug:
                        CM.set_calc('background %s %s' %(pKa.residue.resSeq,state))
                    potentials=self.getAPBSPotentials(pKa,titration,state)
                    #
                    # Assign charges to our residue
                    #
                    self.setCharges(residue, atomnames)
                    #
                    # Return the potentials - same order as in atomnames
                    #
                    energy=self.get_elec_energy(potentials,atomlist)
                    #
                    # We use the bumpscore to effectively exclude a state
                    #
                    if self.routines.getbumpscore(pKa.residue) > 100:
                        energy=100000.0 # State will never be visited
                        self.routines.write('Excluded state\n')
                        self.routines.write(str(pKa.residue)+' '+str(titration)+' '+str(state)+'\n')
                        self.routines.write(self.get_state_name(titration.name,state)+'\n')

                    #energy=energy+self.routines.getbumpscore()
                    #
                    # Add corrections for Asp and Glu trans states.
                    # His tautomers etc.
                    #
                    self.routines.write(self.get_state_name(titration.name,state)+'\n')
                    if self.get_state_name(titration.name,state) in ['ASH1t','ASH2t','GLH1t','GLH2t']:
                        energy=energy+math.log(10)*1.99
                        self.routines.write('Modified energy of trans state\n')
                        self.routines.write(titration.name+'\n')
                        self.routines.write(str(pKa.residue)+'\n')
                        self.routines.write(self.get_state_name(titration.name,state)+'\n')
                    elif self.get_state_name(titration.name,state) in ['JUNKHIS']:
                        energy=energy+0.0
                    #
                    # Done with Background calc for this state
                    #
                    pKa.background[self.get_state_name(titration.name,state)] = energy
                    #
                    # Save it under a unique name
                    #
                    self.routines.write('Saving energy as'+name+'\n')
                    savedict[name]=energy
                    #
                    # Dump the pickle file
                    #
                    with open(backgroundname,'w') as fd:
                        cPickle.dump(savedict,fd)

        with open(self.output_files['background_interaction_energies_file_path'] , 'w') as f:
            keys = savedict.keys()
            keys.sort()
            for key in keys:
                value = savedict[key]
                residue, tit_state = key.rsplit('_', 1)

                f.write(' '.join((residue, tit_state, str(value)))+'\n')
        return

    #
    # --------------------------------
    #

    def calculateDesolvation(self,onlypKa=None):
        """
        #
        #   Calculate the Desolvation Energies
        #
        """

        desolvname=os.path.join(self.state_files, 'desolvation_energies.pickle')
        if os.path.isfile(desolvname):
            with open(desolvname) as fd:
                savedict=cPickle.load(fd)
        else:
            savedict={}
        #
        # ----
        #
        for pKa in self.pKas:
            if onlypKa:
                if not pKa==onlypKa:
                    continue
            residue = pKa.residue
            pKaGroup = pKa.pKaGroup
            ambiguity = pKa.amb

            self.routines.write("-----> Calculating Desolvation Energy for %s %s\n" %(residue.name, residue.resSeq))
            for titration in pKaGroup.DefTitrations:
                #
                # Get all possible states for this group
                #
                possiblestates = titration.allstates
                #
                # Get atoms for potential
                #
                atomnames = self.getAtomsForPotential(pKa,titration)
                atomlist=[]
                for atomname in atomnames:
                    atomlist.append(residue.getAtom(atomname))
                #
                # Switch all the other groups to the neutral reference state
                #
                for other_pKa in self.pKas:
                    if pKa==other_pKa:
                        continue
                    for other_titration in other_pKa.pKaGroup.DefTitrations:
                        #
                        # For each residue we first set all states to False in stateboolean
                        # This means that they cannot be explored during a pKa calculation
                        # Afterwards we set stateboolean to True for the neutral ref state
                        #
                        other_possiblestates = other_titration.allstates
                        for other_state in other_possiblestates:
                            other_pKa.residue.stateboolean[self.get_state_name(other_titration.name, other_state)] = False
                        self.hydrogenRoutines.switchstate('pKa',other_pKa.amb,
                                                          self.get_state_name(other_titration.name,
                                                                              self.neutral_ref_state[other_pKa][other_titration]))
                        other_pKa.residue.stateboolean[self.neutral_ref_state[other_pKa][other_titration]]=True
                #
                # Calculate the self energy for each state
                #
                for state in possiblestates:
                    # Adding a stateboolean structure (dictionary) here,
                    # default values are True, meaning the current protonation state
                    # is allowed to be explored during H-bond optimization. False means not allowed.
                    for state2 in possiblestates:
                        residue.stateboolean[self.get_state_name(titration.name,state2)]=False
                    #
                    # This is the state we are calculating for
                    #
                    residue.stateboolean[self.get_state_name(titration.name, state)] = True
                    name='%s_%s_%s_%s' %(titration.name,pKa.residue.chainID,pKa.residue.resSeq,self.get_state_name(titration.name,state))
                    #
                    if savedict.has_key(name):
                        pKa.desolvation[self.get_state_name(titration.name,state)] = savedict[name]
                        continue
                    self.routines.write("---------> Calculating desolvation energy for residue %s state %s in solvent\n" %(residue.name,self.get_state_name(titration.name,state)))
                    #
                    # Center the map on our set of atoms
                    #
                    center=self.get_atoms_center(atomlist)
                    self.apbs_setup.setfineCenter(center)
                    self.apbs_setup.set_type('desolv')
                    #
                    # Switch to the state
                    # Assign, radii, charges

                    self.hydrogenRoutines.switchstate('pKa', ambiguity, self.get_state_name(titration.name,state))

                    # Not allowing current protonation state to be explored during H-bond optimization
                    #residue.stateboolean[self.get_state_name(titration.name, state)] = False

                    pdb_file_name = os.path.join(self.pdb_dumps_dir, name+'_desolve_input.pdb')


                    self.hbondOptimization()
                    self.dump_protein_file(pdb_file_name)

                    # residue.stateboolean returns to default value (True)
                    #residue.stateboolean[self.get_state_name(titration.name, state)] = True

                    self.zeroAllRadiiCharges()
                    self.setCharges(residue, atomnames)
                    self.setRadii(residue, atomnames)

                    #
                    # Run APBS first time for the state in solvent
                    #
                    if debug:
                        CM.set_calc('Desolv solv %s %s' %(pKa.residue.resSeq,state))

                    solutionEnergy=self.get_elec_energy(self.getAPBSPotentials(pKa,titration,state),atomlist)
                    #
                    # Now we set all radii (= in protein)
                    #
                    self.setAllRadii()
                    #
                    # Run APBS again, - this time for the state in the protein
                    #
                    self.routines.write('--------> Calculating self energy for residue %s %d state %s in the protein\n' %(residue.name,residue.resSeq,self.get_state_name(titration.name,state)))

                    if debug:
                        CM.set_calc('Desolv prot %s %s' %(pKa.residue.resSeq,state))
                    #
                    proteinEnergy = self.get_elec_energy(self.getAPBSPotentials(pKa,titration,state),atomlist)
                    #
                    # Calculate the difference in self energy for this state
                    #
                    desolvation = (proteinEnergy - solutionEnergy)/2.0 # Reaction field energy
                    self.routines.write('Desolvation for %s %d in state %s is %5.3f\n\n'
                          %(residue.name,residue.resSeq,self.get_state_name(titration.name,state),desolvation))
                    self.routines.write( '=======================================\n')
                    pKa.desolvation[self.get_state_name(titration.name,state)] = desolvation
                    self.routines.write('Saving energy as '+name+'\n')
                    savedict[name]=desolvation

                    #
                    # Dump a pickle file
                    #
                    with open(desolvname,'w') as fd:
                        cPickle.dump(savedict,fd)

        with open(self.output_files['desolvation_energies_file_path'], 'w') as f:
            keys = savedict.keys()
            keys.sort()
            for key in keys:
                value = savedict[key]
                residue, tit_state = key.rsplit('_', 1)

                f.write(' '.join((residue, tit_state, str(value)))+'\n')
        return

    #
    # ----
    #

    def init_stateboolean(self):
        """Initialize stateboolean for all residues/titratable groups"""
        for pKa in self.pKas:
            residue = pKa.residue
            pKaGroup = pKa.pKaGroup
            ambiguity = pKa.amb
            for titration in pKaGroup.DefTitrations:
                possiblestates = titration.allstates
                for state in possiblestates:
                    # Adding a stateboolean structure (dictionary) here, default values are True, meaning the current protonation state
                    # is allowed to be explored during H-bond optimization. False means not allowed.
                    if not hasattr(residue,'stateboolean'):
                        residue.stateboolean={}
                    residue.stateboolean[self.get_state_name(titration.name, state)] = True
        return

    #
    # ----
    #

#     def calculate_desolvation_for_residues(self,residues,fix_states={}):
#         """Calculate desolvation for individual residues - not necessarily titratable groups.
#         Do this only for the standard charge state of the residue"""
#         self.findTitratableGroups()
#         #
#         # Define all the residue names
#         #
#         calc_residues=residues[:]
#         for calc_res in calc_residues:
#             for chain in self.protein.getChains():
#                 for residue in chain.get("residues"):
#                     resname = residue.get("name")
#                     name='%s:%s:%s' %(chain.chainID,string.zfill(residue.resSeq,4),resname)
#                     #
#                     # Do we have a match?
#                     #
#                     if calc_res==name:
#                         #
#                         # Yes, calculate desolvation for this residue
#                         #
#                         atomlist=[]
#                         atomnames=[]
#                         for atom in residue.getAtoms():
#                             atomlist.append(atom)
#                             atomnames.append(atom.name)
#                         #
#                         # Calculate the self energy for each this residue in solution and in the protein
#                         #
#                         print "---------> Calculating desolvation energy for residue %s in solvent" %(residue.name)
#                         #
#                         # Center the map on our set of atoms
#                         #
#                         center=self.get_atoms_center(atomlist)
#                         self.apbs_setup.setfineCenter(center)
#                         self.apbs_setup.set_type('desolv')
#                         #
#                         # Add hydrogens
#                         #
#                         self.init_stateboolean() # Initialize stateboolean
#                         #
#                         # this is where we fix the protonation state of some groups, if needed
#                         #
#                         for other_pKa in self.pKas:
#                             resname=other_pKa.residue.__str__()
#                             #print resname
#                             if fix_states.has_key(resname):
#                                 for fix_record in fix_states[resname]:
#                                     fix_titration=fix_record['titgroup']
#                                     fix_state=fix_record['state']
#                                     for other_titration in other_pKa.pKaGroup.DefTitrations:
#                                         #print other_titration.name
#                                         if other_titration.name==fix_titration:
#                                             print 'other_titration',other_titration
#                                             print 'Fixing protonation state of %s to %s' %(other_pKa.residue.__str__(),fix_state)
#                                             self.hydrogenRoutines.switchstate('pKa', other_pKa.amb, fix_state)
#                                             #
#                                             # Disallow all other states during Hbond optimization
#                                             #
#                                             possiblestates = other_titration.allstates
#                                             for state in possiblestates:
#                                                 # Adding a stateboolean structure (dictionary) here, default values are True,
#                                                 # meaning the current protonation state
#                                                 # is allowed to be explored during H-bond optimization. False means not allowed.
#                                                 other_pKa.residue.stateboolean[self.get_state_name(other_titration.name, state)] = False
#                                             other_pKa.residue.stateboolean[state]=True
#                             else:
#                                 #
#                                 # Fix in standard protonation state - this should not be needed
#                                 #
#                                 default_states={'ASP':'ASP',
#                                                 'GLU':'GLU',
#                                                 'ARG':'ARG',
#                                                 'LYS':'LYS',
#                                                 'TYR':'TYR',
#                                                 'NTR':'H3+H2',
#                                                 'CTR':'CTR-'}
#                                 for other_titration in other_pKa.pKaGroup.DefTitrations:
#                                     if default_states.has_key(other_titration.name):
#                                         self.hydrogenRoutines.switchstate('pKa',other_pKa.amb,default_states[other_titration.name])
#                                         #
#                                         # Disallow all other states
#                                         #
#                                         possiblestates = other_titration.allstates
#                                         for state in possiblestates:
#                                             # Adding a stateboolean structure (dictionary) here, default values are True,
#                                             # meaning the current protonation state
#                                             # is allowed to be explored during H-bond optimization. False means not allowed.
#                                             other_pKa.residue.stateboolean[self.get_state_name(other_titration.name, state)] = False
#                                         other_pKa.residue.stateboolean[default_states[other_titration.name]]=True
#                         #
#                         # Fixing done, now optimize and calculate
#                         #
#                         #
#
#                         self.hbondOptimization()
#                         self.zeroAllRadiiCharges()
#                         self.setCharges(residue, atomnames)
#                         self.setRadii(residue, atomnames)
#
#                         #
#                         # Run APBS first time for the state in solvent
#                         #
#                         if debug:
#                             CM.set_calc('Desolv solv %s %s' %(residue.resSeq,state))
#
#                         solutionEnergy=self.get_elec_energy(self.getAPBSPotentials(save_results=False),atomlist)
#                         #
#                         # Now we set all radii (= in protein)
#                         #
#                         self.setAllRadii()
#                         #
#                         # Run APBS again, - this time for the state in the protein
#                         #
#
#                         print '--------> Calculating self energy for residue %d %s  in the protein' %(residue.resSeq,residue.name)
#                         proteinEnergy = self.get_elec_energy(self.getAPBSPotentials(save_results=False),atomlist)
#                         #
#                         # Calculate the difference in self energy for this state
#                         #
#                         desolvation = (proteinEnergy - solutionEnergy)/2.0 # Reaction field energy
#                         print 'Desolvation for %s %d is %5.3f kT'  \
#                               %(residue.name,residue.resSeq,desolvation)
#                         #
#                         # Calculate electrostatic interaction energy
#                         #
#                         #
#                         # Set charges on all other residues
#                         #
#                         self.zeroAllRadiiCharges()
#                         self.setAllRadii()
#                         #
#                         # Here we should define the protonation state we want to use
#                         #
#                         for chain in self.protein.getChains():
#                             for otherresidue in chain.get("residues"):
#                                 if residue == otherresidue:
#                                     continue
#                                 #
#                                 # Get list of all the atoms
#                                 #
#                                 otherlist = []
#                                 for atom in otherresidue.atoms:
#                                     if not atom:
#                                         continue
#                                     if atom.get('name').find('FLIP')==-1:
#                                         otherlist.append(atom.get("name"))
#                                 self.setCharges(otherresidue, otherlist)
#                         #
#                         # Center the map on our residue
#                         #
#                         center=self.get_atoms_center(atomlist)
#
#                         all_center,extent=self.apbs_setup.getCenter()
#                         self.apbs_setup.setfineCenter(center)
#                         self.apbs_setup.set_type('background')
#                         #
#                         # Run APBS
#                         #
#                         if debug:
#                             CM.set_calc('background %s %s' %(residue.resSeq,state))
#                         potentials=self.getAPBSPotentials(save_results=False)
#                         #
#                         # Assign charges to our residue
#                         #
#                         self.setCharges(residue, atomnames)
#                         #
#                         # Return the potentials - same order as in atomnames
#                         #
#                         interaction_energy=self.get_elec_energy(potentials,atomlist)
#
#         print 'Desolvation energy: %5.3f kT' %desolvation
#         print 'Interaction energy: %5.3f kT' %interaction_energy
#         return desolvation, interaction_energy, proteinEnergy/2.0,solutionEnergy/2.0


    #
    # ---------
    #

    def get_atoms_center(self,atomlist):
        #
        # Get the centre of a list of atoms
        #
        minmax={'x':[999.9,-999.9],'y':[999.9,-999.9],'z':[999.9,-999.9]}
        for atom in atomlist:
            if atom:
                for axis in ['x','y','z']:
                    coord=getattr(atom,axis)
                    if coord<minmax[axis][0]:
                        minmax[axis][0]=coord
                    if coord>minmax[axis][1]:
                        minmax[axis][1]=coord
        #
        # Calc the geometric center and extent
        #
        center={}
        extent={}
        for axis in minmax.keys():
            extent[axis]=minmax[axis][1]-minmax[axis][0]
            center[axis]=extent[axis]/2.0+minmax[axis][0]
        return [center['x'],center['y'],center['z']]


    #
    # -------------------------------
    #

    def get_elec_energy(self,potentials,atomlist):
        """
        # Given the electrostatic potential from getAPBSPotentials and a list
        # of atoms, this routine returns the energy in kT
        #
        # This function could be made a lot smarter!! (JN)
        """
        energy=0.0
        count=0
        totphi=0.0
        totcrg=0.0
        netcrg=0.0
        found=0
        #
        # Get the potentials
        #
        for atom in self.protein.getAtoms():
            if not atom:
                continue
            for atom_2 in atomlist:
                if not atom_2:
                    continue
                if is_sameatom(atom,atom_2):
                    totcrg=totcrg+abs(atom.get("ffcharge"))
                    netcrg=netcrg+atom.get("ffcharge")
                    totphi=totphi+abs(potentials[-1][count])
                    energy=energy+(potentials[-1][count])*atom.get("ffcharge")
                    #
                    # Flag that we found an atom
                    #
                    found=found+1
                    break
            #
            # This counter is outside the atom_2 loop!!
            #
            count=count+1
            if found==len(atomlist):
                break
        if abs(totphi)<0.01 or abs(totcrg)<0.01:
            print 'total abs phi',totphi
            print 'total abs crg',totcrg
            print 'net charge   ',netcrg
            PDB2PKAError( 'Something is rotten')

        return energy

    #
    # ----------------------------------
    #

    def getAPBSPotentials(self,group=None,titration=None,state=None,cleanup=True,save_results=False):
        """
            Run APBS and get the potentials

            Returns
                list of potentials (list of floats)
        """
        #
        # Do we have results for this calculation?
        #
        loaded=False
        if save_results:
            result_file=os.path.join(self.phidir,'%s_%s_%s.potentials.pickle' %(self.apbs_setup.type,group.uniqueid,self.get_state_name(titration.name,state)))
            if os.path.isfile(result_file):
                #
                # Yes!
                #
                with open(result_file,'rb') as fd:
                    potentials=cPickle.load(fd)
                    loaded=True
        #
        # Run calc again if needed
        #
        if not loaded:
            apbs_inputfile=self.apbs_setup.printInput()
            self.APBS=runAPBS()
            potentials = self.APBS.runAPBS(self.protein, apbs_inputfile, self.routines, CM)
            if cleanup:
                self.APBS.cleanup()
                self.APBS=None
            if save_results:
                with open(result_file,'wb') as fd:
                    cPickle.dump(potentials,fd)

        return potentials

    #
    # -----
    #

    def getmoreAPBSPotentials(self):
        if not self.APBS:
            PDB2PKAError( 'APBS instance killed')
        return self.APBS.get_potentials(self.protein)

    #
    # ----------------------
    #

    def setRadii(self, residue, atomlist):
        """
            Set the radii for specific atoms in a residue

            Parameters
                residue:  The residue to set (residue)
                atomlist: A list of atomnames (list)
        """
        for atom in residue.getAtoms():
            atomname = atom.get("name")
            if atomname not in atomlist: continue
            charge, radius = self.forcefield.getParams1(residue, atomname)
            if hasattr(atom,'secret_radius'):
                atom.set('radius',atom.secret_radius)
            elif radius != None:
                atom.set("radius", radius)
            else:
                text = "Could not find radius for atom %s" % atomname
                text += " in residue %s %i" % (residue.name, residue.resSeq)
                text += " while attempting to set radius!"
                raise ValueError, text
    #
    # ------------------------------------
    #

    def setCharges(self, residue, atomlist):
        """
            Set the charges for specific atoms in a residue

            Parameters
                residue:  The residue to set (residue)
                atomlist: A list of atomnames (list)
        """
        for atom in residue.getAtoms():
            atomname = atom.get("name")
            if atomname not in atomlist:
                continue
            charge, radius = self.forcefield.getParams1(residue, atomname)

            if hasattr(atom,'secret_charge'):
                atom.set("ffcharge",atom.secret_charge)
            elif charge != None:
                atom.set("ffcharge", charge)
            else:
                text = "Could not find charge for atom %s" % atomname
                text += " in residue %s %i" % (residue.name, residue.resSeq)
                text += " while attempting to set charge!"
                raise ValueError, text
        return
    #
    # ----------------------------
    #

    def setAllRadii(self):
        """
            Set all radii for the entire protein
        """
        for chain in self.protein.getChains():
            for residue in chain.get("residues"):
                for atom in residue.get("atoms"):
                    atomname = atom.get("name")
                    if atomname.find('FLIP')!=-1:
                        continue
                    else:
                        charge, radius = self.forcefield.getParams1(residue, atomname)
                    ###PC

                    if hasattr(atom,'secret_radius'):
                        atom.set("radius",atom.secret_radius)
                    elif radius != None:
                        atom.set("radius", radius)
                    else:
                        if residue.type != 2:
                            text = "Could not find radius for atom %s " % atomname
                            text +="in residue %s %i" % (residue.name, residue.resSeq)
                            text += " while attempting to set all radii!"
                            raise PDB2PKAError(text)
    #
    # -------------------------------
    #

    def zeroAllRadiiCharges(self):
        """
            Set all charges and radii for the protein to zero
        """
        for chain in self.protein.getChains():
            for residue in chain.get("residues"):
                for atom in residue.get("atoms"):
                    atom.set("ffcharge",0.0)
                    atom.set("radius",0.0)
    #
    # --------------------------------
    #

    def getAtomsForPotential(self, pKa,titration, get_neutral_state=None):
        """
        #    Find the atoms that are needed for measuring the potential,
        #    only selecting atoms where the charge changes.
        #    Parameters
        #        pKa:  The pKa object (pKa)
        #    Returns:
        #        atomnames:  A list of atomnames to measure (list)
        """
        neutral_state=None
        atomnames = []
        newatomnames = []
        initialmap = {}
        residue = pKa.residue
        pKaGroup = pKa.pKaGroup
        ambiguity = pKa.amb
        #states = self.hydrogenRoutines.getstates(ambiguity)
        #
        # Change to the start state
        #
        start_state=titration.startstates[0]
        start_state=self.get_state_name(titration.name,start_state)
        self.hydrogenRoutines.switchstate('pKa', ambiguity, start_state)
        sum=0.0
        for atom in residue.getAtoms():
            atomname = atom.get("name")
            if atomname.find('FLIP')!=-1:
                continue

            charge, radius = self.forcefield.getParams1(residue, atomname)
            initialmap[atomname] = charge
            if charge is None:
                print atomname,charge
                print residue.isCterm
                raise PDB2PKAError('Charge on atom is None')
            sum+=charge
        if abs(sum)<0.001:
            neutral_state=start_state
        #
        # Check if charges change in all other states
        #

        for state in titration.endstates+titration.startstates[1:]:
            self.hydrogenRoutines.switchstate('pKa', ambiguity, self.get_state_name(titration.name,state))
            #
            # Check that no charges changed and that no atoms were added
            #
            sum=0.0
            for atom in residue.getAtoms():
                atomname = atom.get("name")
                if atomname.find('FLIP')!=-1:
                    continue

                charge, radius = self.forcefield.getParams1(residue, atomname)
                sum=sum+charge
                if initialmap.has_key(atomname):
                    initcharge = initialmap[atomname]
                    if charge != initcharge:
                        if not atomname in atomnames:
                            atomnames.append(atomname)
                else:
                    if not atomname in atomnames:
                        atomnames.append(atomname)
            #
            # Check that no atoms were removed
            #
            for atom in initialmap.keys():
                if not atom in residue.get('map'):
                    atomnames.append(atom)
        #
        # Make sure that the charges add up to integers by adding extra atoms
        #
        sum=0.01
        while sum>0.001:
            sum=0.0
            added=None
            neutral_state=None
            #
            # Loop over all states to find atoms to add
            #
            for state in titration.endstates+titration.startstates:
                self.hydrogenRoutines.switchstate('pKa', ambiguity, self.get_state_name(titration.name,state))
                #
                # Sum this state
                #
                this_sum=0.0
                for atom in residue.atoms:
                    atomname = atom.get("name")
                    if atomname.find('FLIP')!=-1:
                        continue
                    if not atomname in atomnames:
                        continue
                    charge, radius = self.forcefield.getParams1(residue, atomname)
                    this_sum=this_sum+charge
                #
                # Is this the first neutral state?
                #
                if abs(this_sum)<0.0001:
                    if not neutral_state:
                        neutral_state=state
                #
                # Is this an integer charge?
                #
                diff=float(abs(1000.0*this_sum)-abs(1000.0*int(this_sum)))/1000.0
                sum=sum+diff
                if diff>0.001:
                    #
                    # Find all atoms one bond away
                    #
                    add_atoms=[]
                    for atom in residue.atoms:
                        atomname=atom.get('name')
                        if atomname.find('FLIP')!=-1:
                            continue
                        if not atomname in atomnames:
                            continue
                        #
                        # Add all atoms that are not already added
                        #
                        for bound_atom in atom.bonds:
                            if type(bound_atom) is str:
                                if not bound_atom in atomnames:
                                    add_atoms.append(bound_atom)
                                    added=1
                            else:
                                if not bound_atom.name in atomnames:
                                    add_atoms.append(bound_atom.name)
                                    added=1
                    #
                    # Update atomnames
                    #
                    for addatom in add_atoms:
                        if not addatom in atomnames:
                            atomnames.append(addatom)
                #
                # Next state
                #
                pass
            #
            # Did we add anything?
            #
            if added is None and sum>0.001:
                print sum
                print atomnames
                PDB2PKAError('Could not find integer charge state')
        #
        # Did we just want a neutral state identification?
        #
        if get_neutral_state:
            if not neutral_state:
                PDB2PKAError( "no neutral state for " + str(residue.resSeq))
            return neutral_state
        #
        # No, we wanted the atomnames
        #
        if atomnames==[]:
            print 'Did not find any atoms for ',residue.resSeq
            PDB2PKAError('Something wrong with charges')

        for atomname in atomnames:
            if not atomname in newatomnames:
                newatomnames.append(atomname)
        return newatomnames

    #
    # -------------------------------
    #


    def findTitratableGroups(self):
        """
            Find all titratable groups in the protein based on the definition

            We do a simple name-matching on residue names and titration group names, and
            also a Cterm/Nterm matching.

            We need to build in checks for post-translational modifications

            Returns
                pKalist:  A list of pKa objects (list)
        """
        pKalist = []

        self.routines.write("Finding Titratable groups....\n")
        sys.stdout.flush()
        #
        pKagroupList=self.pKagroups.keys()
        #
        for chain in self.protein.getChains():
            for residue in chain.get("residues"):
                resname = residue.get("name")
                for group in pKagroupList:
                    if resname == group:
                        amb=self.find_hydrogen_amb_for_titgroup(residue,group)
                        thispKa = pKa(residue, self.pKagroups[group], amb)
                        pKalist.append(thispKa)
                        self.routines.write("%s %s\n" % (resname, residue.resSeq), indent=1)
                    elif group=='NTR':
                        if residue.isNterm:
                            #
                            # N-terminus
                            #
                            amb=self.find_hydrogen_amb_for_titgroup(residue,group)
                            thispKa=pKa(residue,self.pKagroups[group],amb)
                            pKalist.append(thispKa)
                            self.routines.write("%s %s\n" % (resname, residue.resSeq), indent=1)
                    elif group=='CTR':
                        if residue.isCterm:
                            #
                            # C-terminus
                            #
                            amb=self.find_hydrogen_amb_for_titgroup(residue,group)
                            thispKa=pKa(residue,self.pKagroups[group],amb)
                            pKalist.append(thispKa)
                            self.routines.write("%s %s\n" % (resname, residue.resSeq), indent=1)
        #
        # Find a neutral state for each group
        #
        self.neutral_ref_state={}
        for this_pka in pKalist:
            residue = this_pka.residue
            pKaGroup = this_pka.pKaGroup
            ambiguity = this_pka.amb
            self.neutral_ref_state[this_pka]={}
            for titration in pKaGroup.DefTitrations:
                neutral_state = self.getAtomsForPotential(this_pka,titration,get_neutral_state=1)
                self.neutral_ref_state[this_pka][titration]=neutral_state
        #
        # Store pKa groups in self.pKas
        #
        self.pKas=pKalist
        return

    #
    # ----------------------------------
    #

    def find_hydrogen_amb_for_titgroup(self,residue,group):
        """Find the hydrogen ambiguity that controls the protonation state for the
        titratable group within the given residue"""
        amb = None
        self.hydrogenRoutines.readHydrogenDefinition()
        for hydrodef in self.hydrogenRoutines.hydrodefs:
            hydname = hydrodef.name
            if hydname == group: # or group in residue.patches: (for ASP/ASH, GLU/GLH)
                amb = hydrogenAmbiguity(residue, hydrodef,self.routines)
            elif group == 'ASP':
                if hydname == 'ASH':
                    amb = hydrogenAmbiguity(residue, hydrodef,self.routines)
                    self.routines.applyPatch('ASH', residue)
            elif group == 'GLU':
                if hydname == 'GLH':
                    amb = hydrogenAmbiguity(residue, hydrodef,self.routines)
                    self.routines.applyPatch('GLH', residue)
        if amb == None:
            text = "Could not find hydrogen ambiguity "
            text += "for titratable group %s!" % group
            raise ValueError, text
        return amb

    #
    # ----------------------------------
    #

    def readTitrationDefinition(self):
        """
            Read the Titration Definition

            Returns:
               mygroups: A dictionary of pKaGroups
        """
        mygroups = {}
        titrationdict = {'ASH1c': '1', 'ASH1t': '2', 'ASH2c': '3', 'ASH2t': '4', 'ASP': '0',
                         'GLH1c': '1', 'GLH1t': '2', 'GLH2c': '3', 'GLH2t': '4', 'GLU': '0',
                         'ARG0': '1+2+3+4', 'ARG': '1+2+3+4+5',
                         'LYS': '1', 'LYS0': '0',
                         'TYR': '1', 'TYR-': '0',
                         'HSD': '1', 'HSE': '2', 'HSP': '1+2',
                         'H3': '1', 'H2': '2', 'H3+H2': '1+2',
                         'CTR01c': '1', 'CTR01t': '2', 'CTR02c': '3', 'CTR02t': '4', 'CTR-': '0'}
        filename = TITRATIONFILE
        if not os.path.isfile(TITRATIONFILE):
            raise ValueError, "Could not find TITRATION.DAT!"

        titration_file = open(filename)

        while 1:
            line=titration_file.readline()
            if line.startswith("//"): pass
            elif line == '': break
            elif line[0]=='*':
                name = ""
                resname = ""
                type = ""
                titrations = []

                name = string.strip(line[1:])
                line = titration_file.readline()
                if line[:8] != 'Residue:':
                    text = "Wrong line found when looking for 'Residue'"
                    raise ValueError, "%s: %s" % (text, line)

                resname = string.strip(string.split(line)[1])

                line = titration_file.readline()
                if line[:10] != 'Grouptype:':
                    text = "Wrong line found when looking for 'Grouptype'"
                    raise ValueError, "%s: %s" % (text, line)

                type = string.lower(string.strip(string.split(line)[1]))
                if type != 'acid' and type != 'base':
                    raise ValueError, 'Group type must be acid or base!'

                line = titration_file.readline()
                while 1:
                    """ Find next transition """
                    #
                    # Skip comments
                    #
                    while line[:2]=='//':
                        line=titration_file.readline()

                    startstates = []
                    endstates = []
                    modelpKa = None

                    if line[:11] != 'Transition:':
                        text = "Wrong line found when looking for 'Transition:'"
                        raise ValueError, "%s: %s" % (text, line)

                    split=string.split(line[11:],'->')
                    for number in string.split(split[0], ','):
                        startstates.append(titrationdict[string.strip(number)])
                    for number in string.split(split[1], ','):
                        endstates.append(titrationdict[string.strip(number)])

                    line = titration_file.readline()
                    #
                    # Skip comments
                    #
                    while line[:2]=='//':
                        line=titration_file.readline()
                    #
                    # Must be the model pKa line
                    #
                    if line[:10]!='Model_pKa:':
                        text = "Wrong line found when looking for 'Model_pKa'"
                        raise ValueError, "%s: %s" % (text, line)

                    modelpKa = float(string.split(line)[1])

                    thisTitration = DefTitration(startstates, endstates,modelpKa,name)
                    titrations.append(thisTitration)

                    line = titration_file.readline()
                    if string.strip(line) == 'END': break

                thisGroup = pKaGroup(name, resname, type, titrations)
                mygroups[name] = thisGroup

                line = titration_file.readline()
                if string.strip(line) == 'END OF FILE': break

        return mygroups

    def get_state_name(self, titrationname, state):
        """
            Get the titration state name from numbers
            Returns: real titration state name as in TITRATION.DAT
        """
        reverse_titrationdict = {}
        if titrationname == 'ASP':
            reverse_titrationdict = {'1': 'ASH1c', '2': 'ASH1t', '3': 'ASH2c', '4': 'ASH2t', '0': 'ASP'}
        elif titrationname == 'GLU':
            reverse_titrationdict = {'1': 'GLH1c', '2': 'GLH1t', '3': 'GLH2c', '4': 'GLH2t', '0': 'GLU'}
        elif titrationname == 'ARG':
            reverse_titrationdict = {'1+2+3+4': 'ARG0', '1+2+3+4+5': 'ARG'}
        elif titrationname == 'LYS':
            reverse_titrationdict = {'1': 'LYS', '0': 'LYS0'}
        elif titrationname == 'TYR':
            reverse_titrationdict = {'1': 'TYR', '0': 'TYR-'}
        elif titrationname == 'HIS':

            reverse_titrationdict = {'1': 'HSD', '2': 'HSE', '1+2': 'HSP'}
        elif titrationname == 'NTR':
            reverse_titrationdict = {'1': 'H3', '2': 'H2', '1+2': 'H3+H2'}
        elif titrationname == 'CTR':
            reverse_titrationdict = {'1': 'CTR01c', '2': 'CTR01t', '3': 'CTR02c', '4': 'CTR02t', '0': 'CTR-'}
        return reverse_titrationdict[state]

#
# -----------------------------------------------
#

def smooth(xdiel,ydiel,zdiel):
    print '\nSmooting dielectric constant using Gaussian filter:\n'

    diel=[xdiel,ydiel,zdiel]
    for d in diel:
        os.system('%s/smooth --format=dx --input=%s --output=%s_smooth.dx --filter=gaussian --stddev=%d --bandwidth=3'%(scriptpath,d,d[:-3],sd))
    xdiel_smooth='%s_smooth.dx' % xdiel[:-3]
    ydiel_smooth='%s_smooth.dx' % ydiel[:-3]
    zdiel_smooth='%s_smooth.dx' % zdiel[:-3]

    return xdiel_smooth, ydiel_smooth, zdiel_smooth
#
# -----------------------------------------------
#

if __name__ == "__main__":
    from pprint import pprint

    def frange(x, y, jump):
        while x < y:
            yield x
            x += jump

    ph_list = list(frange(0.0, 20.01, 0.10))
    ph_count = len(ph_list)

    one_to_zero_list = list(frange(0.0, 1.0, 0.005))
    one_to_zero_list.reverse()
    one_to_zero = dict((ph,charge) for ph, charge in zip(ph_list, one_to_zero_list))

    zero_to_neg_one_list = list(frange(-1.0, 0.0, 0.005))
    zero_to_neg_one_list.reverse()
    zero_to_neg_one = dict((ph,charge) for ph, charge in zip(ph_list, zero_to_neg_one_list))

    class Dummy(object):
        def __init__(self):
            self.warnings = []
            self.ph_at_0_5 = {}



    print "These should pass without issue"
    print "Run acid curve"
    routines = pKaRoutines(None, None, None, None, '', maps = None, sd =None,
                 restart=False, pairene=1.0, test_mode=True)
    routines.find_pH_at_0_5('zero_to_neg_one curve base', zero_to_neg_one, False)

    print "Run base curve"
    routines.find_pH_at_0_5('one_to_zero curve acid', one_to_zero, True)

    print "These should print warnings"
    all_zero_curve = dict((ph,0.0) for ph in ph_list)
    print "Run all zero curves"
    routines.find_pH_at_0_5('All zero curve acid', all_zero_curve, False)
    routines.find_pH_at_0_5('All zero curve base', all_zero_curve, True)


    short_zero_to_one_list = list(frange(0.0, 1.0, 0.010))
    short_one_to_zero_list = short_zero_to_one_list[:]
    short_one_to_zero_list.reverse()
    one_to_zero_and_back = short_one_to_zero_list + short_zero_to_one_list
    one_to_zero_and_back_dict = dict((ph,charge) for ph, charge in zip(ph_list, one_to_zero_and_back))
    routines.find_pH_at_0_5('one_to_zero_and_back curve base', one_to_zero_and_back_dict, True)


    zero_to_neg_one_and_back_dict = dict((ph,charge-1.0) for ph, charge in zip(ph_list, one_to_zero_and_back))
    routines.find_pH_at_0_5('zero_to_neg_one_and_back curve acid', zero_to_neg_one_and_back_dict, False)

    positive_interpolation_curve = {0.0:1.0,
                                    1.0:1.0,
                                    2.0:1.0,
                                    3.0:1.0,
                                    4.0:0.6,
                                    5.0:0.3,
                                    6.0:0.1,
                                    7.0:0.0,
                                    8.0:0.0}

    routines.find_pH_at_0_5('positive_interpolation_curve base', positive_interpolation_curve, True)

    negative_interpolation_curve = {0.0:0.0,
                                    1.0:0.0,
                                    2.0:0.0,
                                    3.0:0.0,
                                    4.0:-0.1,
                                    5.0:-0.3,
                                    6.0:-0.7,
                                    7.0:-1.0,
                                    8.0:-1.0}

    routines.find_pH_at_0_5('negative_interpolation_curve acid', negative_interpolation_curve, False)

    print 'Accumulated warnings:'
    pprint(routines.warnings)
    print 'ph values:'
    pprint(routines.ph_at_0_5)










